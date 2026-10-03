#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/fb.h>
#include <linux/kd.h>
#include <drm/drm.h>
#include <drm/drm_mode.h>
#include <math.h>
#include <pthread.h>
#include <setjmp.h>
#include <sched.h>
#include <stdint.h>
#include <stdio.h>
#include <jpeglib.h>
#include <lzma.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/sysinfo.h>
#include <time.h>
#include <unistd.h>

/*
 * AnimeBootNeo multithread framebuffer player (merged stable branch).
 *
 * Goals:
 *   - do not change bzImage / kernel;
 *   - current ANBF262 raw QOI / ANBJCX2 indexed JPEG chunks only; no historical format fallback;
 *   - a bounded producer/worker pipeline streams encoded QOI/JPEG frames and decodes them in parallel;
 *   - one display thread (main thread) owns /dev/fb0 writes and ordering;
 *   - CLOCK_MONOTONIC uses an absolute no-drop schedule: every encoded frame is presented in order;
 *   - if decode is late, the player catches up without sleeping, but never discards a frame;
 *   - presentation is direct framebuffer copy by design; tearing is accepted for maximum compatibility;
 *   - CLOCK_BOOTTIME instrumentation measures kernel -> player -> first-frame latency in this exploration build;
 *   - direct QOI -> native framebuffer pixel conversion avoids an RGBA staging copy;
 *   - worker count and cache depth adapt to CPUs and available RAM;
 *   - worker pthread stacks are deliberately small for initramfs use.
 */

#define PLAYER_MARKER "AnimeBootNeo-2610.3-FASTFIRST-NODROP-DYNAMIC-JCX2-FBDEBUG8S"
#define ANBF_MAGIC "ANBF262\0"
#define ANBF_HEADER_V2_SIZE 44u
#define ANBF_CODEC_QOI 0u
#define ANBF_CODEC_JPEG 1u
#define QOI_HEADER_SIZE 14u
#define QOI_END_SIZE 8u
#define MAX_WORKERS 16
#define MAX_SLOTS 24
#define MIN_SLOTS 2
#define DEFAULT_PREFETCH_SLOTS 8
#define JCX_MAGIC "ANBJCX2\0"
#define JCX_FIXED_SIZE 40u
#define JCX_ENTRY_SIZE 32u
#define JCX_FLAG_XZ 1u
#define WORKER_STACK (256u * 1024u)
#define JCX_PROGRESS_SLICE (128u * 1024u)
#define NS_PER_SEC 1000000000ull

static const char *g_anim_path = "/AnimeBootNeo/animation.anbf";
static const char *g_anim_jcx_path = "/AnimeBootNeo/animation.anbf.jcx";

struct anbf_frame {
    const uint8_t *data;
    uint32_t size;
};

enum jcx_state { JCX_EMPTY=0, JCX_LOADING=1, JCX_READY=2, JCX_ERROR=3 };

struct jcx_chunk {
    const uint8_t *stored;
    uint32_t stored_size, raw_size, first_frame, frame_count, flags;
    uint8_t *raw;
    uint32_t *frame_offsets;
    uint32_t *frame_sizes;
    enum jcx_state state;
    uint32_t ready_frames; /* frames whose complete JPEG payload is already available */
};

struct pixel_format {
    uint32_t width;
    uint32_t height;
    uint32_t bytespp;
    struct fb_bitfield red, green, blue, transp;
    int fast_bgra32;
    int fast_bgrx32;
    int fast_rgba32;
    int fast_rgbx32;
    int fast_bgr24;
    int fast_rgb24;
};

enum slot_state { SLOT_FREE = 0, SLOT_LOADING = 1, SLOT_ENCODED = 2, SLOT_DECODING = 3, SLOT_READY = 4, SLOT_DISPLAYING = 5 };

struct frame_slot {
    uint8_t *pixels;
    uint8_t *encoded;
    size_t encoded_capacity;
    uint32_t encoded_size;
    uint64_t seq;
    enum slot_state state;
};


struct player_ctx {
    const uint8_t *mapped;
    size_t mapped_size;
    struct anbf_frame *frames;
    uint32_t width, height, frame_count, delay_ms, loops, fit_mode, timing_millihz;
    uint32_t anbf_version, codec;
    uint64_t period_ns;
    uint64_t total_seq;
    int jcx_mode;
    struct jcx_chunk *jcx_chunks;
    uint32_t *jcx_frame_to_chunk;
    uint32_t jcx_chunk_count, jcx_chunk_frames;
    uint32_t jcx_prefetch_chunks;
    int jcx_active_xz, jcx_active_decode;
    int jcx_max_active_xz, jcx_max_active_decode;
    uint64_t jcx_pool_xz_tasks, jcx_pool_decode_tasks, jcx_pool_role_switches;
    uint64_t jcx_scheduler_samples, jcx_runway_total;
    uint32_t jcx_runway_min, jcx_runway_max;
    uint64_t jcx_in_bytes, jcx_out_bytes;
    uint64_t jcx_inflate_count, jcx_inflate_total_ns, jcx_inflate_max_ns;
    uint64_t jcx_wait_count, jcx_wait_total_ns, jcx_wait_max_ns;
    uint64_t jcx_progress_publish_count;
    uint64_t jcx_deadline_wait_count, jcx_deadline_wait_total_ns, jcx_deadline_wait_max_ns;
    uint64_t jcx_ready_immediate_count;

    int fb_fd;
    int drm_mode;
    uint32_t drm_handle, drm_fb_id, drm_connector_id;
    struct drm_mode_crtc drm_saved;
    uint8_t *fb_map;
    size_t fb_map_size;
    struct fb_fix_screeninfo finfo;
    struct fb_var_screeninfo vinfo;
    struct pixel_format pf;
    uint32_t render_width, render_height;
    int scale_down;
    int tty_fd;

    /* Exploration build: playback is direct-only.  The fb timing estimate is
       retained as raw evidence, but it is NOT trusted as the physical panel Hz
       and never changes the source timeline or presentation cadence. */
    uint32_t refresh_millihz;

    struct frame_slot *slots;
    int slot_count;
    int worker_count;
    size_t slot_bytes;
    pthread_t workers[MAX_WORKERS];

    pthread_mutex_t lock;
    pthread_cond_t cond;
    uint64_t next_assign;
    uint64_t next_display;
    int stop;
    int error;
    uint64_t decoded_frames;
    uint64_t displayed_frames;
    uint64_t direct_presents;
    uint64_t max_present_ns;

    /* Streaming smoothness diagnostics. These counters are intentionally
       lightweight and are printed only by the exploration timing build. */
    uint64_t producer_wait_count, producer_wait_total_ns, producer_wait_max_ns;
    uint64_t worker_wait_count, worker_wait_total_ns, worker_wait_max_ns;
    uint64_t display_wait_count, display_wait_total_ns, display_wait_max_ns;
    uint64_t display_wait_xz_ns, display_wait_decode_ns;
    uint64_t xz_frame_read_count, xz_frame_read_total_ns, xz_frame_read_max_ns;
    uint64_t decode_count, decode_total_ns, decode_max_ns;
    uint64_t late_frame_count, late_total_ns, late_max_ns;
    uint64_t present_gap_max_ns, present_gap_total_ns, present_gap_count;
    uint64_t present_gap_over_20ms, present_gap_over_33ms, present_gap_over_50ms, present_gap_over_100ms;
    double present_gap_sq_ms;
    uint64_t last_present_start_ns;
    int peak_encoded_slots, peak_ready_slots;
    int timing_mode;

    uint64_t boot_player_start_ns;
    uint64_t boot_anbf_ready_ns;
    uint64_t boot_fb_ready_ns;
    uint64_t boot_pool_ready_ns;
    uint64_t boot_pipeline_start_ns;
    uint64_t boot_first_encoded_ns;
    uint64_t boot_first_ready_ns;
    uint64_t boot_first_present_ns;
    uint64_t boot_pipeline_end_ns;
};

static uint32_t le32(const uint8_t *p) {
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}
static uint32_t be32(const uint8_t *p) {
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) | ((uint32_t)p[2] << 8) | (uint32_t)p[3];
}
static uint64_t le64(const uint8_t *p) {
    return (uint64_t)le32(p) | ((uint64_t)le32(p+4) << 32);
}
static uint64_t mono_ns(void) {
    struct timespec ts;
    if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0) return 0;
    return (uint64_t)ts.tv_sec * NS_PER_SEC + (uint64_t)ts.tv_nsec;
}
static uint64_t boot_ns(void) {
    struct timespec ts;
#ifdef CLOCK_BOOTTIME
    if (clock_gettime(CLOCK_BOOTTIME, &ts) == 0)
        return (uint64_t)ts.tv_sec * NS_PER_SEC + (uint64_t)ts.tv_nsec;
#endif
    return mono_ns();
}
static void ns_to_ts(uint64_t ns, struct timespec *ts) {
    ts->tv_sec = (time_t)(ns / NS_PER_SEC);
    ts->tv_nsec = (long)(ns % NS_PER_SEC);
}
static void sleep_until_ns(uint64_t target) {
    struct timespec ts;
    ns_to_ts(target, &ts);
    while (clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &ts, NULL) == EINTR) {}
}
static int clamp_int(int v, int lo, int hi) {
    return v < lo ? lo : (v > hi ? hi : v);
}

static int read_cmdline_int(const char *key, int fallback, int lo, int hi) {
    FILE *fp = fopen("/proc/cmdline", "rb");
    if (!fp) return fallback;
    char buf[4096];
    size_t n = fread(buf, 1, sizeof(buf)-1, fp);
    fclose(fp);
    buf[n] = '\0';
    size_t keylen = strlen(key);
    char *save = NULL;
    for (char *tok = strtok_r(buf, " \t\r\n", &save); tok; tok = strtok_r(NULL, " \t\r\n", &save)) {
        if (!strncmp(tok, key, keylen) && tok[keylen] == '=') {
            char *end = NULL;
            long v = strtol(tok + keylen + 1, &end, 10);
            if (end && *end == '\0') return clamp_int((int)v, lo, hi);
        }
    }
    return fallback;
}












static int parse_anbf_header_bytes(struct player_ctx *ctx, const uint8_t *h, size_t available, int compare_only) {
    if (available < ANBF_HEADER_V2_SIZE || memcmp(h, ANBF_MAGIC, 8)) return -2;
    uint32_t version = le32(h+8);
    size_t header_size = 0;
    uint32_t codec;
    if (version == 2u) {
        if (available < ANBF_HEADER_V2_SIZE) return -2;
        codec = le32(h+40);
        if (codec != ANBF_CODEC_QOI && codec != ANBF_CODEC_JPEG) return -2;
        header_size = ANBF_HEADER_V2_SIZE;
    } else return -2;

    uint32_t width=le32(h+12), height=le32(h+16), frame_count=le32(h+20);
    uint32_t delay_ms=le32(h+24), loops=le32(h+28), fit_mode=le32(h+32), timing_millihz=le32(h+36);
    if (!width || !height || width > 8192u || height > 8192u || !frame_count || frame_count > 100000u || !loops || loops > 100000u)
        return -3;
    if (compare_only) {
        if (ctx->anbf_version != version || ctx->codec != codec || ctx->width != width || ctx->height != height ||
            ctx->frame_count != frame_count || ctx->delay_ms != delay_ms || ctx->fit_mode != fit_mode ||
            ctx->timing_millihz != timing_millihz) return -6;
        return (int)header_size;
    }
    ctx->anbf_version=version; ctx->codec=codec; ctx->width=width; ctx->height=height; ctx->frame_count=frame_count;
    ctx->delay_ms=delay_ms; ctx->loops=loops; ctx->fit_mode=fit_mode; ctx->timing_millihz=timing_millihz;
    if (timing_millihz >= 1000u && timing_millihz <= 1000000u)
        ctx->period_ns = 1000000000000ull / (uint64_t)timing_millihz;
    else
        ctx->period_ns = (uint64_t)(delay_ms ? delay_ms : 1u) * 1000000ull;
    if (!ctx->period_ns) ctx->period_ns=1000000ull;
    ctx->total_seq=(uint64_t)frame_count*(uint64_t)loops;
    return (int)header_size;
}




static int parse_anbf_jcx(struct player_ctx *ctx, const char *path) {
    int fd=open(path,O_RDONLY|O_CLOEXEC);
    if (fd<0) return -1;
    struct stat st;
    if (fstat(fd,&st)!=0 || st.st_size<(off_t)JCX_FIXED_SIZE) { close(fd); return -2; }
    ctx->mapped_size=(size_t)st.st_size;
    ctx->mapped=mmap(NULL,ctx->mapped_size,PROT_READ,MAP_PRIVATE,fd,0);
    close(fd);
    if (ctx->mapped==MAP_FAILED) { ctx->mapped=NULL; return -3; }
    const uint8_t *p=ctx->mapped;
    if (memcmp(p,JCX_MAGIC,8)) return -4;
    uint32_t version=le32(p+8), header_size=le32(p+12), frame_count=le32(p+16), chunk_count=le32(p+20);
    uint32_t entry_size=le32(p+24), chunk_frames=le32(p+28);
    uint64_t raw_stream_bytes=le64(p+32); (void)raw_stream_bytes;
    if (version!=2u || header_size!=ANBF_HEADER_V2_SIZE ||
        !frame_count || !chunk_count || entry_size!=JCX_ENTRY_SIZE || !chunk_frames) return -5;
    size_t index_off=JCX_FIXED_SIZE+(size_t)header_size;
    size_t index_bytes=(size_t)chunk_count*JCX_ENTRY_SIZE;
    if (index_off+index_bytes>ctx->mapped_size) return -6;
    int hrc=parse_anbf_header_bytes(ctx,p+JCX_FIXED_SIZE,header_size,0);
    if (hrc<0 || ctx->codec!=ANBF_CODEC_JPEG || ctx->frame_count!=frame_count) return -7;
    ctx->jcx_chunks=calloc(chunk_count,sizeof(*ctx->jcx_chunks));
    ctx->jcx_frame_to_chunk=calloc(frame_count,sizeof(*ctx->jcx_frame_to_chunk));
    if (!ctx->jcx_chunks || !ctx->jcx_frame_to_chunk) return -8;
    uint32_t expected=0;
    for (uint32_t i=0;i<chunk_count;++i) {
        const uint8_t *e=p+index_off+(size_t)i*JCX_ENTRY_SIZE;
        uint64_t off=le64(e); uint32_t stored=le32(e+8), raw=le32(e+12), first=le32(e+16), count=le32(e+20), flags=le32(e+24);
        if (!stored || !raw || !count || first!=expected || first+count>frame_count ||
            off>ctx->mapped_size || stored>ctx->mapped_size-(size_t)off || (flags & ~JCX_FLAG_XZ)) return -9;
        struct jcx_chunk *c=&ctx->jcx_chunks[i];
        c->stored=p+(size_t)off; c->stored_size=stored; c->raw_size=raw; c->first_frame=first; c->frame_count=count; c->flags=flags; c->state=JCX_EMPTY; c->ready_frames=0;
        for (uint32_t f=0;f<count;++f) ctx->jcx_frame_to_chunk[first+f]=i;
        ctx->jcx_in_bytes += stored; ctx->jcx_out_bytes += raw; expected += count;
    }
    if (expected!=frame_count) return -10;
    ctx->jcx_chunk_count=chunk_count; ctx->jcx_chunk_frames=chunk_frames; ctx->jcx_mode=1;
    return 0;
}



static uint32_t qoi_hash(uint8_t r, uint8_t g, uint8_t b, uint8_t a) {
    return (uint32_t)(r * 3u + g * 5u + b * 7u + a * 11u) & 63u;
}

static uint32_t scale_component(uint8_t c, uint32_t bits) {
    if (!bits) return 0;
    uint64_t maxv = bits >= 32 ? 0xffffffffull : ((1ull << bits) - 1ull);
    return (uint32_t)(((uint64_t)c * maxv + 127ull) / 255ull);
}

static inline void put_native(const struct pixel_format *pf, uint8_t *dst,
                              uint8_t r, uint8_t g, uint8_t b, uint8_t a) {
    if (pf->fast_bgra32) { dst[0]=b; dst[1]=g; dst[2]=r; dst[3]=a; return; }
    if (pf->fast_bgrx32) { dst[0]=b; dst[1]=g; dst[2]=r; dst[3]=0; return; }
    if (pf->fast_rgba32) { dst[0]=r; dst[1]=g; dst[2]=b; dst[3]=a; return; }
    if (pf->fast_rgbx32) { dst[0]=r; dst[1]=g; dst[2]=b; dst[3]=0; return; }
    if (pf->fast_bgr24) { dst[0]=b; dst[1]=g; dst[2]=r; return; }
    if (pf->fast_rgb24) { dst[0]=r; dst[1]=g; dst[2]=b; return; }
    uint32_t v = 0;
    v |= scale_component(r, pf->red.length) << pf->red.offset;
    v |= scale_component(g, pf->green.length) << pf->green.offset;
    v |= scale_component(b, pf->blue.length) << pf->blue.offset;
    if (pf->transp.length) v |= scale_component(a, pf->transp.length) << pf->transp.offset;
    for (uint32_t i=0; i<pf->bytespp; ++i) dst[i] = (uint8_t)(v >> (i*8));
}

static int decode_qoi_generic(const uint8_t *data, size_t size, const struct pixel_format *pf,
                              uint8_t *out, size_t out_size) {
    if (!data || size < QOI_HEADER_SIZE + QOI_END_SIZE || memcmp(data, "qoif", 4)) return -1;
    uint32_t w = be32(data+4), h = be32(data+8);
    if (w != pf->width || h != pf->height) return -2;
    uint64_t pixels = (uint64_t)w * (uint64_t)h;
    uint64_t needed = pixels * pf->bytespp;
    if (needed > out_size) return -3;

    uint8_t index[64][4];
    memset(index, 0, sizeof(index));
    uint8_t r=0, g=0, b=0, a=255;
    size_t p = QOI_HEADER_SIZE;
    uint64_t px = 0;
    uint32_t run = 0;

    while (px < pixels) {
        if (run) {
            --run;
        } else {
            if (p >= size - QOI_END_SIZE) return -4;
            uint8_t b1 = data[p++];
            if (b1 == 0xFE) {
                if (p + 3 > size - QOI_END_SIZE) return -4;
                r=data[p++]; g=data[p++]; b=data[p++];
            } else if (b1 == 0xFF) {
                if (p + 4 > size - QOI_END_SIZE) return -4;
                r=data[p++]; g=data[p++]; b=data[p++]; a=data[p++];
            } else {
                switch (b1 & 0xC0) {
                    case 0x00: {
                        uint8_t *q = index[b1 & 0x3F]; r=q[0]; g=q[1]; b=q[2]; a=q[3]; break;
                    }
                    case 0x40:
                        r = (uint8_t)(r + ((b1 >> 4) & 0x03) - 2);
                        g = (uint8_t)(g + ((b1 >> 2) & 0x03) - 2);
                        b = (uint8_t)(b + (b1 & 0x03) - 2);
                        break;
                    case 0x80: {
                        if (p >= size - QOI_END_SIZE) return -4;
                        uint8_t b2 = data[p++];
                        int dg = (int)(b1 & 0x3F) - 32;
                        int drdg = (int)((b2 >> 4) & 0x0F) - 8;
                        int dbdg = (int)(b2 & 0x0F) - 8;
                        r = (uint8_t)(r + dg + drdg);
                        g = (uint8_t)(g + dg);
                        b = (uint8_t)(b + dg + dbdg);
                        break;
                    }
                    case 0xC0:
                        run = (uint32_t)(b1 & 0x3F);
                        break;
                }
            }
        }
        uint8_t *idx = index[qoi_hash(r,g,b,a)]; idx[0]=r; idx[1]=g; idx[2]=b; idx[3]=a;
        put_native(pf, out + (size_t)px * pf->bytespp, r,g,b,a);
        ++px;
    }
    return 0;
}


static int decode_qoi_bgra32(const uint8_t *data, size_t size, const struct pixel_format *pf,
                             uint8_t *out, size_t out_size, int keep_alpha) {
    if (!data || size < QOI_HEADER_SIZE + QOI_END_SIZE || memcmp(data, "qoif", 4)) return -1;
    uint32_t w=be32(data+4), h=be32(data+8);
    if (w!=pf->width || h!=pf->height) return -2;
    uint64_t pixels=(uint64_t)w*(uint64_t)h;
    if (pixels*4u > out_size) return -3;
    uint8_t index[64][4]; memset(index,0,sizeof(index));
    uint8_t r=0,g=0,b=0,a=255; size_t p=QOI_HEADER_SIZE; uint64_t px=0; uint32_t run=0;
    uint32_t *dst=(uint32_t*)out;
    while (px<pixels) {
        if (run) --run;
        else {
            if (p>=size-QOI_END_SIZE) return -4;
            uint8_t b1=data[p++];
            if (b1==0xFE) { if (p+3>size-QOI_END_SIZE) return -4; r=data[p++];g=data[p++];b=data[p++]; }
            else if (b1==0xFF) { if (p+4>size-QOI_END_SIZE) return -4; r=data[p++];g=data[p++];b=data[p++];a=data[p++]; }
            else switch (b1&0xC0) {
                case 0x00: { uint8_t *q=index[b1&0x3F];r=q[0];g=q[1];b=q[2];a=q[3];break; }
                case 0x40: r=(uint8_t)(r+((b1>>4)&3)-2);g=(uint8_t)(g+((b1>>2)&3)-2);b=(uint8_t)(b+(b1&3)-2);break;
                case 0x80: { if (p>=size-QOI_END_SIZE) return -4; uint8_t b2=data[p++];int dg=(int)(b1&0x3F)-32;int drdg=(int)((b2>>4)&0x0F)-8;int dbdg=(int)(b2&0x0F)-8;r=(uint8_t)(r+dg+drdg);g=(uint8_t)(g+dg);b=(uint8_t)(b+dg+dbdg);break; }
                case 0xC0: run=(uint32_t)(b1&0x3F);break;
            }
        }
        uint8_t *q=index[qoi_hash(r,g,b,a)];q[0]=r;q[1]=g;q[2]=b;q[3]=a;
        dst[px++]=(uint32_t)b | ((uint32_t)g<<8) | ((uint32_t)r<<16) | ((uint32_t)(keep_alpha?a:0)<<24);
    }
    return 0;
}

static int decode_qoi_native(const uint8_t *data, size_t size, const struct pixel_format *pf,
                             uint8_t *out, size_t out_size) {
    if (pf->fast_bgra32) return decode_qoi_bgra32(data,size,pf,out,out_size,1);
    if (pf->fast_bgrx32) return decode_qoi_bgra32(data,size,pf,out,out_size,0);
    return decode_qoi_generic(data,size,pf,out,out_size);
}

struct jpeg_error_ctx {
    struct jpeg_error_mgr pub;
    jmp_buf jump;
};

static void anbf_jpeg_error_exit(j_common_ptr cinfo) {
    struct jpeg_error_ctx *err = (struct jpeg_error_ctx*)cinfo->err;
    longjmp(err->jump, 1);
}

static int decode_jpeg_native(const uint8_t *data, size_t size, const struct pixel_format *pf,
                              uint8_t *out, size_t out_size) {
    if (!data || size < 4 || data[0] != 0xFF || data[1] != 0xD8) return -20;
    uint64_t needed=(uint64_t)pf->width*(uint64_t)pf->height*pf->bytespp;
    if (needed > out_size) return -21;

    struct jpeg_decompress_struct cinfo;
    struct jpeg_error_ctx jerr;
    memset(&cinfo,0,sizeof(cinfo));
    cinfo.err=jpeg_std_error(&jerr.pub); jerr.pub.error_exit=anbf_jpeg_error_exit;
    if (setjmp(jerr.jump)) { jpeg_destroy_decompress(&cinfo); return -22; }
    jpeg_create_decompress(&cinfo);
    jpeg_mem_src(&cinfo,(unsigned char*)data,(unsigned long)size);
    if (jpeg_read_header(&cinfo,TRUE)!=JPEG_HEADER_OK) { jpeg_destroy_decompress(&cinfo); return -23; }

    int direct_native=0, components=3;
#ifdef JCS_EXTENSIONS
    if (pf->fast_bgra32) { cinfo.out_color_space=JCS_EXT_BGRA; direct_native=1; components=4; }
    else if (pf->fast_bgrx32) { cinfo.out_color_space=JCS_EXT_BGRX; direct_native=1; components=4; }
    else if (pf->fast_rgba32) { cinfo.out_color_space=JCS_EXT_RGBA; direct_native=1; components=4; }
    else if (pf->fast_rgbx32) { cinfo.out_color_space=JCS_EXT_RGBX; direct_native=1; components=4; }
    else if (pf->fast_bgr24) { cinfo.out_color_space=JCS_EXT_BGR; direct_native=1; components=3; }
    else if (pf->fast_rgb24) { cinfo.out_color_space=JCS_RGB; direct_native=1; components=3; }
    else cinfo.out_color_space=JCS_RGB;
#else
    cinfo.out_color_space=JCS_RGB;
#endif
    cinfo.dct_method=JDCT_IFAST;
    if (!jpeg_start_decompress(&cinfo)) { jpeg_destroy_decompress(&cinfo); return -24; }
    if (cinfo.output_width!=pf->width || cinfo.output_height!=pf->height) {
        jpeg_abort_decompress(&cinfo); jpeg_destroy_decompress(&cinfo); return -25;
    }
    if (direct_native && cinfo.output_components!=components) direct_native=0;

    if (direct_native) {
        size_t row_stride=(size_t)cinfo.output_width*(size_t)components;
        if (row_stride != (size_t)pf->width*pf->bytespp) direct_native=0;
        if (direct_native) {
            while (cinfo.output_scanline<cinfo.output_height) {
                JSAMPROW row=out+(size_t)cinfo.output_scanline*row_stride;
                if (jpeg_read_scanlines(&cinfo,&row,1)!=1) { jpeg_abort_decompress(&cinfo);jpeg_destroy_decompress(&cinfo);return -26; }
            }
        }
    }
    if (!direct_native) {
        if (cinfo.output_components!=3) { jpeg_abort_decompress(&cinfo);jpeg_destroy_decompress(&cinfo);return -25; }
        const size_t row_stride=(size_t)cinfo.output_width*3u;
        JSAMPARRAY rows=(*cinfo.mem->alloc_sarray)((j_common_ptr)&cinfo,JPOOL_IMAGE,(JDIMENSION)row_stride,1);
        uint32_t y=0;
        while (cinfo.output_scanline<cinfo.output_height) {
            if (jpeg_read_scanlines(&cinfo,rows,1)!=1) { jpeg_abort_decompress(&cinfo);jpeg_destroy_decompress(&cinfo);return -26; }
            const uint8_t *src=rows[0]; uint8_t *dst=out+(size_t)y*pf->width*pf->bytespp;
            for (uint32_t x=0;x<pf->width;++x)
                put_native(pf,dst+(size_t)x*pf->bytespp,src[(size_t)x*3u],src[(size_t)x*3u+1u],src[(size_t)x*3u+2u],255u);
            ++y;
        }
    }
    if (!jpeg_finish_decompress(&cinfo)) { jpeg_destroy_decompress(&cinfo); return -27; }
    jpeg_destroy_decompress(&cinfo); return 0;
}

static int parse_anbf(struct player_ctx *ctx, const char *path) {
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0) { fprintf(stderr, "AnimeBoot Neo Debug: open %s failed: %s\n", path, strerror(errno)); return -1; }
    struct stat st;
    if (fstat(fd, &st) != 0 || st.st_size < (off_t)ANBF_HEADER_V2_SIZE) { close(fd); return -1; }
    ctx->mapped_size = (size_t)st.st_size;
    ctx->mapped = mmap(NULL, ctx->mapped_size, PROT_READ, MAP_PRIVATE, fd, 0);
    close(fd);
    if (ctx->mapped == MAP_FAILED) { ctx->mapped=NULL; return -1; }
    (void)madvise((void*)ctx->mapped, ctx->mapped_size, MADV_SEQUENTIAL);

    const uint8_t *h = ctx->mapped;
    if (memcmp(h, ANBF_MAGIC, 8)) return -2;
    ctx->anbf_version = le32(h+8);
    size_t header_size;
    if (ctx->anbf_version == 2u) {
        if (ctx->mapped_size < ANBF_HEADER_V2_SIZE) return -2;
        ctx->codec = le32(h+40);
        if (ctx->codec != ANBF_CODEC_QOI && ctx->codec != ANBF_CODEC_JPEG) return -2;
        header_size = ANBF_HEADER_V2_SIZE;
    } else {
        return -2;
    }
    ctx->width = le32(h+12); ctx->height = le32(h+16); ctx->frame_count = le32(h+20);
    ctx->delay_ms = le32(h+24); ctx->loops = le32(h+28); ctx->fit_mode = le32(h+32);
    ctx->timing_millihz = le32(h+36);
    if (!ctx->width || !ctx->height || ctx->width > 8192u || ctx->height > 8192u ||
        !ctx->frame_count || ctx->frame_count > 100000u || !ctx->loops) return -3;
    if (ctx->loops > 100000u) return -3;
    if (ctx->timing_millihz >= 1000u && ctx->timing_millihz <= 1000000u)
        ctx->period_ns = 1000000000000ull / (uint64_t)ctx->timing_millihz;
    else
        ctx->period_ns = (uint64_t)(ctx->delay_ms ? ctx->delay_ms : 1u) * 1000000ull;
    if (!ctx->period_ns) ctx->period_ns = 1000000ull;
    ctx->total_seq = (uint64_t)ctx->frame_count * (uint64_t)ctx->loops;

    ctx->frames = calloc(ctx->frame_count, sizeof(*ctx->frames));
    if (!ctx->frames) return -4;
    size_t pos = header_size;
    for (uint32_t i=0; i<ctx->frame_count; ++i) {
        if (pos + 4 > ctx->mapped_size) return -5;
        uint32_t sz = le32(ctx->mapped + pos); pos += 4;
        if (pos + (size_t)sz > ctx->mapped_size) return -5;
        if (ctx->codec == ANBF_CODEC_QOI) {
            if (sz < QOI_HEADER_SIZE + QOI_END_SIZE || memcmp(ctx->mapped + pos, "qoif", 4)) return -5;
        } else {
            if (sz < 4 || ctx->mapped[pos] != 0xFF || ctx->mapped[pos+1] != 0xD8) return -5;
        }
        ctx->frames[i].data = ctx->mapped + pos;
        ctx->frames[i].size = sz;
        pos += sz;
    }
    return 0;
}

#include "animeboot_drm.h"
#include "animeboot_debug_font.h"

static int open_framebuffer(struct player_ctx *ctx) {
    const char *paths[] = {"/dev/fb0", "/dev/graphics/fb0", NULL};
    for (int i=0; paths[i]; ++i) {
        ctx->fb_fd = open(paths[i], O_RDWR | O_CLOEXEC);
        if (ctx->fb_fd >= 0) break;
    }
    if (ctx->fb_fd < 0 && neo_drm_open(ctx)<0) { fprintf(stderr, "AnimeBoot Neo Debug: fbdev/DRM display unavailable\n"); return -1; }
    if (!ctx->drm_mode && (ioctl(ctx->fb_fd, FBIOGET_FSCREENINFO, &ctx->finfo) < 0 ||
        ioctl(ctx->fb_fd, FBIOGET_VSCREENINFO, &ctx->vinfo) < 0)) return -2;
    uint32_t bpp = ctx->vinfo.bits_per_pixel;
    if (bpp != 16 && bpp != 24 && bpp != 32) {
        fprintf(stderr, "AnimeBoot Neo Debug: unsupported fb bpp=%u\n", bpp); return -3;
    }
    /* Prefer 1:1 output.  If firmware/GRUB had to fall back below the ANBF
       canvas, keep aspect ratio and scale down instead of aborting the boot
       animation.  Normal matching-resolution playback stays on the fast path. */
    ctx->render_width = ctx->width;
    ctx->render_height = ctx->height;
    ctx->scale_down = 0;
    if (ctx->vinfo.xres < ctx->width || ctx->vinfo.yres < ctx->height) {
        uint64_t lhs = (uint64_t)ctx->vinfo.xres * (uint64_t)ctx->height;
        uint64_t rhs = (uint64_t)ctx->vinfo.yres * (uint64_t)ctx->width;
        if (lhs <= rhs) {
            ctx->render_width = ctx->vinfo.xres;
            ctx->render_height = (uint32_t)(((uint64_t)ctx->height * ctx->render_width) / ctx->width);
        } else {
            ctx->render_height = ctx->vinfo.yres;
            ctx->render_width = (uint32_t)(((uint64_t)ctx->width * ctx->render_height) / ctx->height);
        }
        if (!ctx->render_width) ctx->render_width = 1;
        if (!ctx->render_height) ctx->render_height = 1;
        ctx->scale_down = 1;
        fprintf(stderr, "AnimeBoot Neo Debug: fb %ux%u below ANBF %ux%u; fallback render %ux%u (aspect-preserving)\n",
                ctx->vinfo.xres, ctx->vinfo.yres, ctx->width, ctx->height,
                ctx->render_width, ctx->render_height);
    }
    ctx->fb_map_size = ctx->finfo.smem_len;
    if (!ctx->fb_map_size) ctx->fb_map_size = (size_t)ctx->finfo.line_length * ctx->vinfo.yres_virtual;
    if (!ctx->drm_mode) ctx->fb_map = mmap(NULL, ctx->fb_map_size, PROT_READ|PROT_WRITE, MAP_SHARED, ctx->fb_fd, 0);
    if (ctx->fb_map == MAP_FAILED) { ctx->fb_map=NULL; return -5; }
    if (!ctx->drm_mode) (void)ioctl(ctx->fb_fd, FBIOBLANK, FB_BLANK_UNBLANK);

    /* Keep the firmware/fbdev timing calculation strictly as a diagnostic
       hint.  Two real 60 Hz panels have already reported ~76-77 Hz here, so
       this value must never be used as a physical refresh authority. */
    {
        uint64_t htotal=(uint64_t)ctx->vinfo.xres + ctx->vinfo.left_margin + ctx->vinfo.right_margin + ctx->vinfo.hsync_len;
        uint64_t vtotal=(uint64_t)ctx->vinfo.yres + ctx->vinfo.upper_margin + ctx->vinfo.lower_margin + ctx->vinfo.vsync_len;
        if (ctx->vinfo.pixclock && htotal && vtotal) {
            uint64_t mhz=1000000000000000ull / ((uint64_t)ctx->vinfo.pixclock * htotal * vtotal);
            if (mhz >= 20000ull && mhz <= 360000ull) ctx->refresh_millihz=(uint32_t)mhz;
        }
    }

    struct pixel_format *pf = &ctx->pf;
    memset(pf, 0, sizeof(*pf));
    pf->width=ctx->width; pf->height=ctx->height; pf->bytespp=bpp/8;
    pf->red=ctx->vinfo.red; pf->green=ctx->vinfo.green; pf->blue=ctx->vinfo.blue; pf->transp=ctx->vinfo.transp;
    pf->fast_bgra32 = bpp==32 && pf->red.offset==16 && pf->red.length==8 && pf->green.offset==8 && pf->green.length==8 && pf->blue.offset==0 && pf->blue.length==8 && pf->transp.offset==24 && pf->transp.length==8;
    pf->fast_bgrx32 = bpp==32 && pf->red.offset==16 && pf->red.length==8 && pf->green.offset==8 && pf->green.length==8 && pf->blue.offset==0 && pf->blue.length==8 && pf->transp.length==0;
    pf->fast_rgba32 = bpp==32 && pf->red.offset==0 && pf->red.length==8 && pf->green.offset==8 && pf->green.length==8 && pf->blue.offset==16 && pf->blue.length==8 && pf->transp.offset==24 && pf->transp.length==8;
    pf->fast_rgbx32 = bpp==32 && pf->red.offset==0 && pf->red.length==8 && pf->green.offset==8 && pf->green.length==8 && pf->blue.offset==16 && pf->blue.length==8 && pf->transp.length==0;
    pf->fast_bgr24 = bpp==24 && pf->red.offset==16 && pf->green.offset==8 && pf->blue.offset==0;
    pf->fast_rgb24 = bpp==24 && pf->red.offset==0 && pf->green.offset==8 && pf->blue.offset==16;
    ctx->slot_bytes = (size_t)ctx->width * ctx->height * pf->bytespp;
    uint32_t xpad=(ctx->vinfo.xres > ctx->render_width) ? (ctx->vinfo.xres-ctx->render_width)/2u : 0u;
    uint32_t ypad=(ctx->vinfo.yres > ctx->render_height) ? (ctx->vinfo.yres-ctx->render_height)/2u : 0u;
    size_t row_need=(size_t)(ctx->vinfo.xoffset+xpad+ctx->render_width)*pf->bytespp;
    size_t end_need=(size_t)(ctx->vinfo.yoffset+ypad+ctx->render_height-1u)*ctx->finfo.line_length + row_need;
    if (ctx->finfo.line_length < row_need || end_need > ctx->fb_map_size) {
        fprintf(stderr, "AnimeBoot Neo Debug: framebuffer geometry/stride is inconsistent\n"); return -6;
    }
    if (xpad || ypad || ctx->scale_down) {
        for (uint32_t y=0; y<ctx->vinfo.yres; ++y) {
            size_t off=(size_t)(ctx->vinfo.yoffset+y)*ctx->finfo.line_length + (size_t)ctx->vinfo.xoffset*pf->bytespp;
            size_t len=(size_t)ctx->vinfo.xres*pf->bytespp;
            if (off+len <= ctx->fb_map_size) memset(ctx->fb_map+off,0,len);
        }
    }

    ctx->tty_fd = open("/dev/tty0", O_RDWR | O_CLOEXEC);
    if (ctx->tty_fd >= 0) (void)ioctl(ctx->tty_fd, KDSETMODE, KD_GRAPHICS);
    return 0;
}

static int choose_workers(struct player_ctx *ctx, long cpus) {
    int w;
    if (ctx->jcx_mode) {
        /* R8: one unified CPU worker pool.  Every worker can dynamically take
           either an urgent Chunked-XZ inflate task or a JPEG decode task.
           Use the online CPU count directly (with the existing hard safety
           cap); odd CPU counts are supported rather than assumed impossible. */
        w=(int)cpus;
        if (w<1) w=1;
        if (w>MAX_WORKERS) w=MAX_WORKERS;
        /* Prefer an even-sized dynamic pool when more than one CPU is online.
           Real systems are usually even-threaded, but odd counts remain safe:
           one logical CPU is left to the display/PID1 side instead of creating
           an asymmetric pool.  A true single-core machine still uses one. */
        if (w>1 && (w&1)) --w;
        ctx->jcx_prefetch_chunks=(uint32_t)(w+1);
        return read_cmdline_int("AnimeBootNeoPlayerWorkers",w,1,MAX_WORKERS);
    }
    /* Whole-XZ JPEG and Raw QOI retain the proven codec-aware worker policy.
       Chunked JPEG alone uses the R8 unified dynamic pool above. */
    if (ctx->codec == ANBF_CODEC_JPEG) {
        if (cpus <= 2) w=1; else if (cpus <= 4) w=3; else if (cpus <= 8) w=5; else if (cpus <= 16) w=7; else w=8;
    } else {
        if (cpus <= 2) w=1; else if (cpus <= 4) w=3; else if (cpus <= 8) w=5; else if (cpus <= 16) w=7; else w=8;
    }
    return read_cmdline_int("AnimeBootNeoPlayerWorkers", w, 1, MAX_WORKERS);
}

static int configure_pool(struct player_ctx *ctx) {
    long cpus = sysconf(_SC_NPROCESSORS_ONLN);
    if (cpus < 1) cpus = 1;
    int workers = choose_workers(ctx,cpus);
    int auto_slots = ctx->jcx_mode ? (workers + 4) : (workers + (ctx->codec==ANBF_CODEC_QOI ? 7 : 6));
    if (auto_slots < DEFAULT_PREFETCH_SLOTS) auto_slots = DEFAULT_PREFETCH_SLOTS;
    if (ctx->codec==ANBF_CODEC_QOI && auto_slots < 10) auto_slots = 10;
    int desired_slots = read_cmdline_int("AnimeBootNeoPlayerBuffers", auto_slots, MIN_SLOTS, MAX_SLOTS);
    struct sysinfo si;
    uint64_t ram = 0;
    if (sysinfo(&si) == 0) ram = (uint64_t)si.totalram * (uint64_t)si.mem_unit;
    uint64_t cap = ram ? ram / 12u : 128ull*1024ull*1024ull;
    if (cap < 64ull*1024ull*1024ull) cap = 64ull*1024ull*1024ull;
    if (cap > 192ull*1024ull*1024ull) cap = 192ull*1024ull*1024ull;
    int slots = desired_slots;
    while (slots > MIN_SLOTS && (uint64_t)ctx->slot_bytes * (uint64_t)slots > cap) --slots;
    if (ctx->total_seq < (uint64_t)slots) slots = (int)ctx->total_seq;
    if (slots < 1) slots = 1;
    if (workers >= slots) workers = slots - 1;
    if (ctx->jcx_mode && workers>1 && (workers&1)) --workers;
    if (workers < 1) workers = 1;
    if (slots == 1 && ctx->total_seq > 1) slots = 2;
    ctx->slot_count=slots; ctx->worker_count=workers;

    ctx->slots = calloc((size_t)slots, sizeof(*ctx->slots));
    if (!ctx->slots) return -1;
    for (int i=0; i<slots; ++i) {
        void *p=NULL;
        if (posix_memalign(&p, 64, ctx->slot_bytes) != 0 || !p) return -2;
        ctx->slots[i].pixels=(uint8_t*)p; ctx->slots[i].state=SLOT_FREE;
    }
    return 0;
}

static struct frame_slot *find_free_slot(struct player_ctx *ctx) {
    for (int i=0; i<ctx->slot_count; ++i) if (ctx->slots[i].state==SLOT_FREE) return &ctx->slots[i];
    return NULL;
}
static struct frame_slot *find_encoded_slot(struct player_ctx *ctx) {
    struct frame_slot *best=NULL;
    for (int i=0; i<ctx->slot_count; ++i) {
        struct frame_slot *slot=&ctx->slots[i];
        if (slot->state != SLOT_ENCODED) continue;
        if (!best || slot->seq < best->seq) best=slot;
    }
    return best;
}

static struct frame_slot *find_seq_slot(struct player_ctx *ctx, uint64_t seq) {
    for (int i=0; i<ctx->slot_count; ++i) if (ctx->slots[i].state==SLOT_READY && ctx->slots[i].seq==seq) return &ctx->slots[i];
    return NULL;
}

static struct frame_slot *find_any_seq_slot(struct player_ctx *ctx, uint64_t seq) {
    for (int i=0; i<ctx->slot_count; ++i)
        if (ctx->slots[i].state!=SLOT_FREE && ctx->slots[i].seq==seq) return &ctx->slots[i];
    return NULL;
}

static int count_slot_state_locked(struct player_ctx *ctx, enum slot_state state) {
    int count=0;
    for (int i=0; i<ctx->slot_count; ++i) if (ctx->slots[i].state==state) ++count;
    return count;
}

static void update_queue_peaks_locked(struct player_ctx *ctx) {
    int encoded=count_slot_state_locked(ctx,SLOT_ENCODED);
    int ready=count_slot_state_locked(ctx,SLOT_READY);
    if (encoded > ctx->peak_encoded_slots) ctx->peak_encoded_slots=encoded;
    if (ready > ctx->peak_ready_slots) ctx->peak_ready_slots=ready;
}

static void add_wait_stat(uint64_t elapsed, uint64_t *count, uint64_t *total, uint64_t *maximum) {
    if (!elapsed) return;
    ++*count;
    *total += elapsed;
    if (elapsed > *maximum) *maximum=elapsed;
}
static struct frame_slot *find_latest_ready_leq(struct player_ctx *ctx, uint64_t minimum, uint64_t maximum) {
    struct frame_slot *best=NULL;
    for (int i=0; i<ctx->slot_count; ++i) {
        struct frame_slot *slot=&ctx->slots[i];
        if (slot->state != SLOT_READY || slot->seq < minimum || slot->seq > maximum) continue;
        if (!best || slot->seq > best->seq) best=slot;
    }
    return best;
}
static void free_ready_before_locked(struct player_ctx *ctx, uint64_t seq) {
    for (int i=0; i<ctx->slot_count; ++i) {
        if (ctx->slots[i].state == SLOT_READY && ctx->slots[i].seq < seq)
            ctx->slots[i].state = SLOT_FREE;
    }
}
static int consecutive_ready(struct player_ctx *ctx, uint64_t start, int maximum) {
    int count=0;
    for (; count<maximum; ++count) if (!find_seq_slot(ctx, start+(uint64_t)count)) break;
    return count;
}




static int jcx_publish_available(struct player_ctx *ctx, uint32_t ci, size_t produced,
                                 size_t *parse_pos_io, uint32_t *published_io) {
    struct jcx_chunk *c=&ctx->jcx_chunks[ci];
    size_t pos=*parse_pos_io; uint32_t published=*published_io;
    uint32_t before=published;
    while (published<c->frame_count) {
        if (produced < pos+4u) break;
        uint32_t sz=le32(c->raw+pos);
        if (!sz || pos+4u+(size_t)sz>c->raw_size) return -43;
        if (produced < pos+4u+(size_t)sz) break;
        c->frame_offsets[published]=(uint32_t)(pos+4u);
        c->frame_sizes[published]=sz;
        pos += 4u+(size_t)sz;
        ++published;
    }
    if (published!=before) {
        pthread_mutex_lock(&ctx->lock);
        /* raw bytes and frame index entries are fully written before publishing
           ready_frames under the same mutex used by JPEG consumers. */
        c->ready_frames=published;
        ctx->jcx_progress_publish_count += (uint64_t)(published-before);
        if (ci==0 && published>0 && !ctx->boot_first_encoded_ns) ctx->boot_first_encoded_ns=boot_ns();
        pthread_cond_broadcast(&ctx->cond);
        pthread_mutex_unlock(&ctx->lock);
    }
    *parse_pos_io=pos; *published_io=published;
    return 0;
}

static int jcx_inflate_chunk(struct player_ctx *ctx, uint32_t ci) {
    struct jcx_chunk *c=&ctx->jcx_chunks[ci];
    uint8_t *raw=malloc(c->raw_size);
    uint32_t *offs=calloc(c->frame_count,sizeof(uint32_t));
    uint32_t *sizes=calloc(c->frame_count,sizeof(uint32_t));
    if (!raw || !offs || !sizes) { free(raw);free(offs);free(sizes); return -40; }

    pthread_mutex_lock(&ctx->lock);
    c->raw=raw; c->frame_offsets=offs; c->frame_sizes=sizes; c->ready_frames=0;
    pthread_mutex_unlock(&ctx->lock);

    uint64_t t0=mono_ns();
    size_t produced=0, parse_pos=0; uint32_t published=0;
    if (c->flags & JCX_FLAG_XZ) {
        lzma_stream strm=LZMA_STREAM_INIT;
        lzma_ret lr=lzma_stream_decoder(&strm,UINT64_MAX,0);
        if (lr!=LZMA_OK) return -41;
        strm.next_in=c->stored; strm.avail_in=c->stored_size;
        for (;;) {
            size_t remain=c->raw_size-produced;
            if (!remain) { lzma_end(&strm); return -41; }
            size_t slice=remain<JCX_PROGRESS_SLICE?remain:JCX_PROGRESS_SLICE;
            strm.next_out=raw+produced; strm.avail_out=slice;
            lr=lzma_code(&strm,LZMA_FINISH);
            size_t wrote=slice-strm.avail_out;
            produced += wrote;
            int pr=jcx_publish_available(ctx,ci,produced,&parse_pos,&published);
            if (pr) { lzma_end(&strm); return pr; }
            if (lr==LZMA_STREAM_END) break;
            if (lr!=LZMA_OK || (!wrote && !strm.avail_in)) { lzma_end(&strm); return -41; }
        }
        size_t remaining_in=strm.avail_in;
        lzma_end(&strm);
        if (remaining_in || produced!=c->raw_size) return -41;
    } else {
        memcpy(raw,c->stored,c->raw_size); produced=c->raw_size;
        int pr=jcx_publish_available(ctx,ci,produced,&parse_pos,&published);
        if (pr) return pr;
    }
    if (published!=c->frame_count || parse_pos!=c->raw_size) return -45;
    uint64_t elapsed=mono_ns()-t0;
    pthread_mutex_lock(&ctx->lock);
    c->state=JCX_READY;
    ++ctx->jcx_inflate_count; ctx->jcx_inflate_total_ns+=elapsed; if(elapsed>ctx->jcx_inflate_max_ns)ctx->jcx_inflate_max_ns=elapsed;
    pthread_cond_broadcast(&ctx->cond); pthread_mutex_unlock(&ctx->lock);
    return 0;
}

static uint32_t jcx_current_chunk_locked(struct player_ctx *ctx) {
    uint32_t fi=(uint32_t)(ctx->next_display % ctx->frame_count);
    return ctx->jcx_frame_to_chunk ? ctx->jcx_frame_to_chunk[fi] : 0u;
}

static int jcx_payload_ready_locked(struct player_ctx *ctx, uint64_t seq,
                                    const uint8_t **data, size_t *size) {
    if (!ctx->jcx_frame_to_chunk || seq>=ctx->total_seq) return 0;
    uint32_t fi=(uint32_t)(seq % ctx->frame_count);
    uint32_t ci=ctx->jcx_frame_to_chunk[fi];
    struct jcx_chunk *c=&ctx->jcx_chunks[ci];
    uint32_t local=fi-c->first_frame;
    if (!c->raw || local>=c->ready_frames) return 0;
    if (data) *data=c->raw+c->frame_offsets[local];
    if (size) *size=c->frame_sizes[local];
    return 1;
}

static uint32_t jcx_ready_runway_locked(struct player_ctx *ctx, uint64_t start_seq) {
    uint32_t limit=ctx->jcx_chunk_frames ? ctx->jcx_chunk_frames*3u : 96u;
    if (limit<32u) limit=32u;
    uint32_t count=0;
    while (count<limit && start_seq+(uint64_t)count<ctx->total_seq) {
        if (!jcx_payload_ready_locked(ctx,start_seq+(uint64_t)count,NULL,NULL)) break;
        ++count;
    }
    ++ctx->jcx_scheduler_samples;
    ctx->jcx_runway_total += count;
    if (ctx->jcx_scheduler_samples==1 || count<ctx->jcx_runway_min) ctx->jcx_runway_min=count;
    if (count>ctx->jcx_runway_max) ctx->jcx_runway_max=count;
    return count;
}

/* Dynamic XZ share for the unified pool.  On a four-thread machine this
   deliberately oscillates between 2XZ+2JPEG when runway is low, 1XZ+3JPEG
   while the prefetch window is healthy, and 0XZ+4JPEG while it is very full.
   The same ratios scale automatically to other online CPU counts. */
static int jcx_desired_xz_locked(struct player_ctx *ctx, uint32_t runway) {
    int pool=ctx->worker_count;
    if (pool<=1) return 1;
    uint32_t low=ctx->jcx_chunk_frames/2u;
    if (low<8u) low=8u;
    uint32_t medium=ctx->jcx_chunk_frames;
    uint32_t high=ctx->jcx_chunk_frames*2u;
    if (!jcx_payload_ready_locked(ctx,ctx->next_assign,NULL,NULL)) {
        int target=(pool+1)/2;
        if (target<1) target=1;
        return target;
    }
    if (runway<low) {
        int target=(pool+1)/2;
        if (target<1) target=1;
        return target;
    }
    if (runway<medium) {
        int target=pool/4;
        if (target<1) target=1;
        return target;
    }
    if (runway<high) return 1;
    return 0;
}

/* Deadline-aware chunk chooser: never scan from chunk 0.  Always claim the
   chunk containing the next frame to present first, then walk forward only
   inside a bounded look-ahead window.  Combined with progressive publishing,
   this prevents a far-future chunk from delaying the next required JPEG. */
static uint32_t jcx_choose_claim_locked(struct player_ctx *ctx) {
    if (!ctx->jcx_chunk_count) return UINT32_MAX;
    uint32_t current=jcx_current_chunk_locked(ctx);
    uint32_t limit=current+ctx->jcx_prefetch_chunks;
    if (limit>=ctx->jcx_chunk_count) limit=ctx->jcx_chunk_count-1u;
    for (uint32_t i=current;i<=limit;++i) {
        if (ctx->jcx_chunks[i].state==JCX_EMPTY) return i;
    }
    return UINT32_MAX;
}

enum unified_task_kind { UTASK_NONE=0, UTASK_XZ=1, UTASK_JPEG=2 };

static void *jcx_unified_worker_main(void *opaque) {
    struct player_ctx *ctx=(struct player_ctx*)opaque;
    int last_kind=UTASK_NONE;
    for (;;) {
        enum unified_task_kind kind=UTASK_NONE;
        uint32_t claim=UINT32_MAX;
        struct frame_slot *slot=NULL;
        uint64_t seq=0;
        const uint8_t *encoded_data=NULL;
        size_t encoded_size=0;

        pthread_mutex_lock(&ctx->lock);
        while (!ctx->stop && !ctx->error) {
            struct frame_slot *free_slot=find_free_slot(ctx);
            int decode_available=0;
            if (free_slot && ctx->next_assign<ctx->total_seq)
                decode_available=jcx_payload_ready_locked(ctx,ctx->next_assign,&encoded_data,&encoded_size);

            uint32_t runway=jcx_ready_runway_locked(ctx,ctx->next_assign);
            int desired_xz=jcx_desired_xz_locked(ctx,runway);
            claim=jcx_choose_claim_locked(ctx);
            int readyq=count_slot_state_locked(ctx,SLOT_READY);

            /* Keep the display fed first.  If READY depth is low and the next
               JPEG payload already exists, decode it immediately. */
            if (decode_available && readyq+ctx->jcx_active_decode<2) {
                kind=UTASK_JPEG;
            } else if (claim!=UINT32_MAX && ctx->jcx_active_xz<desired_xz) {
                kind=UTASK_XZ;
            } else if (decode_available) {
                kind=UTASK_JPEG;
            } else if (claim!=UINT32_MAX && ctx->jcx_active_xz==0) {
                /* Forward progress safety: if the next JPEG is not available,
                   always allow at least one worker to become an XZ worker. */
                kind=UTASK_XZ;
            }

            if (kind==UTASK_JPEG) {
                seq=ctx->next_assign++;
                slot=free_slot;
                slot->seq=seq;
                slot->state=SLOT_DECODING;
                ++ctx->jcx_active_decode;
                if (ctx->jcx_active_decode>ctx->jcx_max_active_decode) ctx->jcx_max_active_decode=ctx->jcx_active_decode;
                ++ctx->jcx_pool_decode_tasks;
                break;
            }
            if (kind==UTASK_XZ) {
                ctx->jcx_chunks[claim].state=JCX_LOADING;
                ++ctx->jcx_active_xz;
                if (ctx->jcx_active_xz>ctx->jcx_max_active_xz) ctx->jcx_max_active_xz=ctx->jcx_active_xz;
                ++ctx->jcx_pool_xz_tasks;
                break;
            }
            if (ctx->next_assign>=ctx->total_seq) {
                pthread_mutex_unlock(&ctx->lock);
                return NULL;
            }
            pthread_cond_wait(&ctx->cond,&ctx->lock);
        }
        int stop=ctx->stop||ctx->error;
        pthread_mutex_unlock(&ctx->lock);
        if (stop) break;

        if (last_kind!=UTASK_NONE && last_kind!=(int)kind) {
            pthread_mutex_lock(&ctx->lock); ++ctx->jcx_pool_role_switches; pthread_mutex_unlock(&ctx->lock);
        }
        last_kind=(int)kind;

        if (kind==UTASK_XZ) {
            int rc=jcx_inflate_chunk(ctx,claim);
            pthread_mutex_lock(&ctx->lock);
            if (ctx->jcx_active_xz>0) --ctx->jcx_active_xz;
            if (rc) { ctx->jcx_chunks[claim].state=JCX_ERROR; ctx->error=rc; ctx->stop=1; }
            pthread_cond_broadcast(&ctx->cond);
            pthread_mutex_unlock(&ctx->lock);
            if (rc) break;
            continue;
        }

        /* The payload was published before the sequence was claimed and never
           moves for the lifetime of the chunk, so it is safe to decode after
           dropping the scheduler mutex. */
        uint64_t decode_start=mono_ns();
        int rc=decode_jpeg_native(encoded_data,encoded_size,&ctx->pf,slot->pixels,ctx->slot_bytes);
        uint64_t decode_elapsed=mono_ns()-decode_start;
        pthread_mutex_lock(&ctx->lock);
        if (ctx->jcx_active_decode>0) --ctx->jcx_active_decode;
        ++ctx->decode_count; ctx->decode_total_ns+=decode_elapsed; if(decode_elapsed>ctx->decode_max_ns)ctx->decode_max_ns=decode_elapsed;
        if (rc) { ctx->error=rc; ctx->stop=1; slot->state=SLOT_FREE; }
        else { slot->state=SLOT_READY; ++ctx->decoded_frames; update_queue_peaks_locked(ctx); }
        pthread_cond_broadcast(&ctx->cond);
        pthread_mutex_unlock(&ctx->lock);
        if (rc) break;
    }
    return NULL;
}

static void *worker_main(void *opaque) {
    struct player_ctx *ctx=(struct player_ctx*)opaque;
    for (;;) {
        pthread_mutex_lock(&ctx->lock);
        struct frame_slot *slot=NULL;
        while (!ctx->stop && ctx->next_assign < ctx->total_seq && !(slot=find_free_slot(ctx)))
            pthread_cond_wait(&ctx->cond,&ctx->lock);
        if(ctx->stop || ctx->next_assign>=ctx->total_seq){pthread_mutex_unlock(&ctx->lock);break;}
        uint64_t seq=ctx->next_assign++;
        slot->seq=seq;slot->state=SLOT_DECODING;
        pthread_mutex_unlock(&ctx->lock);
        uint32_t fi=(uint32_t)(seq % ctx->frame_count);
        uint64_t decode_start=mono_ns();
        int rc=(ctx->codec==ANBF_CODEC_JPEG)
            ? decode_jpeg_native(ctx->frames[fi].data,ctx->frames[fi].size,&ctx->pf,slot->pixels,ctx->slot_bytes)
            : decode_qoi_native(ctx->frames[fi].data,ctx->frames[fi].size,&ctx->pf,slot->pixels,ctx->slot_bytes);
        uint64_t elapsed=mono_ns()-decode_start;
        pthread_mutex_lock(&ctx->lock);
        ++ctx->decode_count;ctx->decode_total_ns+=elapsed;
        if(elapsed>ctx->decode_max_ns)ctx->decode_max_ns=elapsed;
        if(rc){ctx->error=rc;ctx->stop=1;slot->state=SLOT_FREE;}
        else{slot->state=SLOT_READY;++ctx->decoded_frames;update_queue_peaks_locked(ctx);}
        pthread_cond_broadcast(&ctx->cond);pthread_mutex_unlock(&ctx->lock);
        if(rc)break;
    }
    return NULL;
}

static int start_workers(struct player_ctx *ctx) {
    pthread_attr_t attr; pthread_attr_init(&attr);
    (void)pthread_attr_setstacksize(&attr, WORKER_STACK);
    int made=0;
    for (int i=0; i<ctx->worker_count; ++i) {
        void *(*entry)(void *) = ctx->jcx_mode ? jcx_unified_worker_main : worker_main;
        if (pthread_create(&ctx->workers[i], &attr, entry, ctx) != 0) break;
        ++made;
    }
    pthread_attr_destroy(&attr);
    if (!made) return -1;
    ctx->worker_count=made;
    return 0;
}

static void copy_frame_to_fb(struct player_ctx *ctx, const uint8_t *src, uint32_t target_yoffset) {
    const uint32_t bpp=ctx->pf.bytespp;
    size_t src_stride=(size_t)ctx->width * bpp;
    uint32_t rw=ctx->render_width, rh=ctx->render_height;
    uint32_t xpad=(ctx->vinfo.xres > rw) ? (ctx->vinfo.xres-rw)/2u : 0u;
    uint32_t ypad=(ctx->vinfo.yres > rh) ? (ctx->vinfo.yres-rh)/2u : 0u;
    size_t base=(size_t)(target_yoffset+ypad)*ctx->finfo.line_length + (size_t)(ctx->vinfo.xoffset+xpad)*bpp;

    if (!ctx->scale_down) {
        if (xpad==0 && ypad==0 && ctx->vinfo.xoffset==0 && target_yoffset==0 && ctx->finfo.line_length==src_stride) {
            memcpy(ctx->fb_map, src, ctx->slot_bytes);
            return;
        }
        for (uint32_t y=0; y<ctx->height; ++y) {
            size_t dstoff=base+(size_t)y*ctx->finfo.line_length;
            if (dstoff+src_stride > ctx->fb_map_size) break;
            memcpy(ctx->fb_map+dstoff, src+(size_t)y*src_stride, src_stride);
        }
        return;
    }

    for (uint32_t dy=0; dy<rh; ++dy) {
        uint32_t sy=(uint32_t)(((uint64_t)dy * ctx->height) / rh);
        const uint8_t *srow=src+(size_t)sy*src_stride;
        size_t dstoff=base+(size_t)dy*ctx->finfo.line_length;
        if (dstoff+(size_t)rw*bpp > ctx->fb_map_size) break;
        uint8_t *drow=ctx->fb_map+dstoff;
        for (uint32_t dx=0; dx<rw; ++dx) {
            uint32_t sx=(uint32_t)(((uint64_t)dx * ctx->width) / rw);
            memcpy(drow+(size_t)dx*bpp, srow+(size_t)sx*bpp, bpp);
        }
    }
}

static void present_frame(struct player_ctx *ctx, const uint8_t *src) {
    uint64_t present_start=mono_ns();
    if (ctx->last_present_start_ns) {
        uint64_t gap=present_start-ctx->last_present_start_ns;
        if (gap > ctx->present_gap_max_ns) ctx->present_gap_max_ns=gap;
        ctx->present_gap_total_ns += gap; ++ctx->present_gap_count;
        double gap_ms=(double)gap/1000000.0; ctx->present_gap_sq_ms += gap_ms*gap_ms;
        if (gap > 20000000ull) ++ctx->present_gap_over_20ms;
        if (gap > 33000000ull) ++ctx->present_gap_over_33ms;
        if (gap > 50000000ull) ++ctx->present_gap_over_50ms;
        if (gap > 100000000ull) ++ctx->present_gap_over_100ms;
    }
    ctx->last_present_start_ns=present_start;
    /* Direct framebuffer presentation is deliberate in this branch.  The
       target FPS is a decoder/presentation ceiling, not a display-refresh
       synchronisation promise; tearing may occur on single-buffer EFI fbdev. */
    copy_frame_to_fb(ctx,src,ctx->vinfo.yoffset);
    neo_drm_flush(ctx);
    ++ctx->direct_presents;
    uint64_t elapsed=mono_ns()-present_start;
    if (elapsed > ctx->max_present_ns) ctx->max_present_ns=elapsed;
}


static int run_pipeline(struct player_ctx *ctx) {
    ctx->boot_pipeline_start_ns=boot_ns();
    if (pthread_mutex_init(&ctx->lock,NULL) || pthread_cond_init(&ctx->cond,NULL)) return -1;
    if (start_workers(ctx) != 0) {
        pthread_mutex_lock(&ctx->lock); ctx->stop=1; pthread_cond_broadcast(&ctx->cond); pthread_mutex_unlock(&ctx->lock);
        return -2;
    }

    /* FASTFIRST: wait only for frame 0, then present it immediately. */
    pthread_mutex_lock(&ctx->lock);
    while (!ctx->error && !find_seq_slot(ctx,0))
        pthread_cond_wait(&ctx->cond,&ctx->lock);
    if (ctx->error) { pthread_mutex_unlock(&ctx->lock); return -3; }
    struct frame_slot *first=find_seq_slot(ctx,0);
    if (!first) { pthread_mutex_unlock(&ctx->lock); return -3; }
    first->state=SLOT_DISPLAYING;
    ctx->boot_first_ready_ns=boot_ns();
    pthread_mutex_unlock(&ctx->lock);

    present_frame(ctx,first->pixels);
    ++ctx->displayed_frames;
    ctx->boot_first_present_ns=boot_ns();

    /* Absolute no-drop schedule.  Frame 0 occupies the first period.  If a
       later frame is ready early, wait for its absolute deadline.  If decode
       finishes late, present immediately and continue without rebasing the
       rest of the time axis.  No sequence is deliberately skipped. */
    uint64_t timeline_start=mono_ns();

    pthread_mutex_lock(&ctx->lock);
    ctx->next_display=1;
    first->state=SLOT_FREE;
    pthread_cond_broadcast(&ctx->cond);
    pthread_mutex_unlock(&ctx->lock);

    for (uint64_t seq=1; !ctx->error && seq<ctx->total_seq; ++seq) {
        pthread_mutex_lock(&ctx->lock);
        struct frame_slot *slot=NULL;
        while (!ctx->error && !(slot=find_seq_slot(ctx,seq))) {
            int waiting_on_xz = 0;
            if (ctx->jcx_mode) waiting_on_xz=!jcx_payload_ready_locked(ctx,seq,NULL,NULL);
            struct frame_slot *pending=find_any_seq_slot(ctx,seq);
            if (pending && pending->state==SLOT_LOADING) waiting_on_xz=1;
            else if (pending && (pending->state==SLOT_ENCODED || pending->state==SLOT_DECODING)) waiting_on_xz=0;
            uint64_t wait_start=mono_ns();
            pthread_cond_wait(&ctx->cond,&ctx->lock);
            uint64_t wait_elapsed=mono_ns()-wait_start;
            add_wait_stat(wait_elapsed,&ctx->display_wait_count,&ctx->display_wait_total_ns,&ctx->display_wait_max_ns);
            if (waiting_on_xz) ctx->display_wait_xz_ns += wait_elapsed;
            else ctx->display_wait_decode_ns += wait_elapsed;
        }
        if (ctx->error) { pthread_mutex_unlock(&ctx->lock); break; }
        slot->state=SLOT_DISPLAYING;
        pthread_mutex_unlock(&ctx->lock);

        uint64_t target=timeline_start + seq*ctx->period_ns;
        uint64_t now=mono_ns();
        if (target > now) {
            sleep_until_ns(target);
        } else {
            uint64_t late=now-target;
            if (late > 1000000ull) {
                ++ctx->late_frame_count;
                ctx->late_total_ns += late;
                if (late > ctx->late_max_ns) ctx->late_max_ns=late;
            }
        }

        present_frame(ctx,slot->pixels);
        ++ctx->displayed_frames;

        pthread_mutex_lock(&ctx->lock);
        ctx->next_display=seq+1u;
        slot->state=SLOT_FREE;
        pthread_cond_broadcast(&ctx->cond);
        pthread_mutex_unlock(&ctx->lock);
    }

    /* Preserve the nominal N/F duration when the machine is fast enough.  A
       genuinely slow decoder is allowed to extend total duration rather than
       discard frames. */
    if (!ctx->error) {
        uint64_t nominal_end=timeline_start + ctx->total_seq*ctx->period_ns;
        uint64_t now=mono_ns();
        if (nominal_end > now) sleep_until_ns(nominal_end);
    }

    pthread_mutex_lock(&ctx->lock);
    ctx->stop=1;
    pthread_cond_broadcast(&ctx->cond);
    pthread_mutex_unlock(&ctx->lock);
    for (int i=0;i<ctx->worker_count;++i) pthread_join(ctx->workers[i],NULL);
    ctx->boot_pipeline_end_ns=boot_ns();
    return ctx->error ? -4 : 0;
}

static uint64_t read_uptime_file_ns(const char *path) {
    FILE *fp=fopen(path,"rb");
    if (!fp) return 0;
    double seconds=0.0;
    int ok=fscanf(fp,"%lf",&seconds);
    fclose(fp);
    if (ok!=1 || seconds<0.0) return 0;
    return (uint64_t)(seconds*1000000000.0 + 0.5);
}

static double ms_between(uint64_t later, uint64_t earlier) {
    if (!later || !earlier || later<earlier) return -1.0;
    return (double)(later-earlier)/1000000.0;
}

static double ms_since_boot(uint64_t when) {
    return when ? (double)when/1000000.0 : -1.0;
}

/* The counters below come from the original offline-debug player. Two pages
 * expose measured startup and playback data, never estimated GRUB/disk times.
 * Keyboard skipping during animation/diagnostics is intentionally not enabled.
 */
struct debug_surface {
    struct player_ctx *ctx;
    uint8_t white[4],heading[4];
};
static void debug_put_pixel(void *opaque,uint32_t x,uint32_t y,int heading) {
    struct debug_surface *surface=opaque;
    struct player_ctx *ctx=surface->ctx;
    size_t off=(size_t)(y+ctx->vinfo.yoffset)*ctx->finfo.line_length+
               (size_t)(x+ctx->vinfo.xoffset)*ctx->pf.bytespp;
    if(off+ctx->pf.bytespp<=ctx->fb_map_size)
        memcpy(ctx->fb_map+off,heading?surface->heading:surface->white,ctx->pf.bytespp);
}
static void debug_draw_page(struct player_ctx *ctx,const char *page,int page_number) {
    if(!ctx->fb_map)return;
    /* Retain our active DRM dumb buffer and KD_GRAPHICS.  fbcon may not be
     * attached to this framebuffer at all: no tty or console restoration. */
    for(uint32_t y=0;y<ctx->vinfo.yres;++y) {
        size_t off=(size_t)(y+ctx->vinfo.yoffset)*ctx->finfo.line_length;
        size_t bytes=(size_t)ctx->vinfo.xres*ctx->pf.bytespp;
        if(off+bytes<=ctx->fb_map_size)memset(ctx->fb_map+off,0,bytes);
    }
    struct debug_surface surface={0};surface.ctx=ctx;
    put_native(&ctx->pf,surface.white,230,238,255,255);
    put_native(&ctx->pf,surface.heading,page_number==1?60:255,page_number==1?215:195,page_number==1?255:45,255);
    neo_debug_draw_text(&surface,ctx->vinfo.xres,ctx->vinfo.yres,page,debug_put_pixel);
    neo_drm_flush(ctx);
}

static void timing_framebuffer_pause(struct player_ctx *ctx, int pipeline_rc) {
    if (!ctx->timing_mode) return;
    double playback_ms=ms_between(ctx->boot_pipeline_end_ns,ctx->boot_first_present_ns);
    double effective_fps=(playback_ms>0.0)?((double)ctx->displayed_frames*1000.0/playback_ms):0.0;
    double source_fps=1000000000.0/(double)ctx->period_ns;
    const char *mode=ctx->jcx_mode?"JPEG Chunk32 dynamic":"QOI Raw";
    char page[4096];
    snprintf(page,sizeof(page),
        "AnimeBoot Neo Debug (2610.3) - STARTUP [1/2]\n\n"
        "Result: %s   codec: %s\n"
        "Framebuffer: %s  %ux%u  %u bpp  stride=%u\n"
        "CPU=%ld  workers=%d  buffers=%d  cache=%.1f MiB\n\n"
        "Kernel -> player entry : %.3f ms\n"
        "Read animation header  : %.3f ms\n"
        "Initialize framebuffer : %.3f ms\n"
        "Allocate buffer pool   : %.3f ms\n"
        "Pipeline -> first frame: %.3f ms\n"
        "Kernel -> first frame  : %.3f ms\n\n"
        "Kernel clock excludes firmware/GRUB file loading.\n"
        "No Linux disk discovery or writes are required.\n"
        "Next page in 8 seconds.\n",
        pipeline_rc==0?"OK":"ERROR",mode,ctx->finfo.id,ctx->vinfo.xres,ctx->vinfo.yres,
        ctx->vinfo.bits_per_pixel,ctx->finfo.line_length,sysconf(_SC_NPROCESSORS_ONLN),
        ctx->worker_count,ctx->slot_count,(double)ctx->slot_bytes*ctx->slot_count/(1024.0*1024.0),
        ms_since_boot(ctx->boot_player_start_ns),ms_between(ctx->boot_anbf_ready_ns,ctx->boot_player_start_ns),
        ms_between(ctx->boot_fb_ready_ns,ctx->boot_anbf_ready_ns),ms_between(ctx->boot_pool_ready_ns,ctx->boot_fb_ready_ns),
        ms_between(ctx->boot_first_present_ns,ctx->boot_pipeline_start_ns),ms_since_boot(ctx->boot_first_present_ns));
    debug_draw_page(ctx,page,1);
    fputs(page,stderr); fflush(stderr);
    sleep_until_ns(mono_ns()+8*NS_PER_SEC);
    snprintf(page,sizeof(page),
        "AnimeBoot Neo Debug (2610.3) - PLAYBACK [2/2]\n\n"
        "Target %.3f FPS   frames=%u   loops=%u\n"
        "Playback elapsed: %.3f ms   effective: %.3f FPS\n"
        "Decoded / shown : %llu / %llu\n"
        "Late frames (>1 ms): %llu   max lateness: %.3f ms\n"
        "Max presentation gap: %.3f ms   max framebuffer copy: %.3f ms\n"
        "Gaps >33 / >50 / >100 ms: %llu / %llu / %llu\n\n"
        "Display wait total: %.3f ms  (XZ %.3f / decode %.3f)\n"
        "Decode total across workers: %.3f ms   max one: %.3f ms\n"
        "Direct framebuffer, no deliberate frame dropping.\n"
        "Debug display time is excluded from playback FPS.\n\n"
        "Reboot to Windows in 8 seconds. Esc skip is not active here.\n",
        source_fps,ctx->frame_count,ctx->loops,playback_ms,effective_fps,
        (unsigned long long)ctx->decoded_frames,(unsigned long long)ctx->displayed_frames,
        (unsigned long long)ctx->late_frame_count,(double)ctx->late_max_ns/1e6,
        (double)ctx->present_gap_max_ns/1e6,(double)ctx->max_present_ns/1e6,
        (unsigned long long)ctx->present_gap_over_33ms,(unsigned long long)ctx->present_gap_over_50ms,
        (unsigned long long)ctx->present_gap_over_100ms,(double)ctx->display_wait_total_ns/1e6,
        (double)ctx->display_wait_xz_ns/1e6,(double)ctx->display_wait_decode_ns/1e6,
        (double)ctx->decode_total_ns/1e6,(double)ctx->decode_max_ns/1e6);
    debug_draw_page(ctx,page,2);
    fputs(page,stderr); fflush(stderr);
    sleep_until_ns(mono_ns()+8*NS_PER_SEC);
}

static void cleanup(struct player_ctx *ctx, int restore_text) {
    if (restore_text) neo_drm_restore(ctx);
    if (restore_text && ctx->tty_fd>=0) (void)ioctl(ctx->tty_fd,KDSETMODE,KD_TEXT);
    if (ctx->tty_fd>=0) close(ctx->tty_fd);
    if (ctx->fb_map) munmap(ctx->fb_map,ctx->fb_map_size);
    if (ctx->fb_fd>=0) close(ctx->fb_fd);
    if (ctx->slots) { for (int i=0;i<ctx->slot_count;++i) { free(ctx->slots[i].pixels); free(ctx->slots[i].encoded); } free(ctx->slots); }
    free(ctx->frames);
    if (ctx->jcx_chunks) { for(uint32_t i=0;i<ctx->jcx_chunk_count;++i){free(ctx->jcx_chunks[i].raw);free(ctx->jcx_chunks[i].frame_offsets);free(ctx->jcx_chunks[i].frame_sizes);} free(ctx->jcx_chunks); }
    free(ctx->jcx_frame_to_chunk);
    if (ctx->mapped) munmap((void*)ctx->mapped,ctx->mapped_size);
}

static int selftest_anbf(const char *path) {
    struct player_ctx c; memset(&c,0,sizeof(c)); c.fb_fd=-1; c.tty_fd=-1;
    int rc=parse_anbf(&c,path);
    if (rc) { fprintf(stderr,"SELFTEST parse rc=%d\n",rc); cleanup(&c,1); return 2; }
    c.pf.width=c.width; c.pf.height=c.height; c.pf.bytespp=4; c.pf.fast_bgra32=1;
    size_t bytes=(size_t)c.width*c.height*4u;
    uint8_t *buf=malloc(bytes);
    if (!buf) { cleanup(&c,1); return 3; }
    uint64_t checksum=1469598103934665603ull;
    for (uint32_t i=0;i<c.frame_count;++i) {
        rc = (c.codec == ANBF_CODEC_JPEG)
            ? decode_jpeg_native(c.frames[i].data,c.frames[i].size,&c.pf,buf,bytes)
            : decode_qoi_native(c.frames[i].data,c.frames[i].size,&c.pf,buf,bytes);
        if (rc) { fprintf(stderr,"SELFTEST codec=%u frame=%u rc=%d\n",c.codec,i,rc); free(buf); cleanup(&c,1); return 4; }
        /* Sparse FNV sample keeps selftest quick while proving decoded output exists. */
        size_t step=bytes/1024u+1u;
        for (size_t p=0;p<bytes;p+=step) { checksum^=buf[p]; checksum*=1099511628211ull; }
    }
    printf("%s SELFTEST OK v=%u codec=%s %ux%u frames=%u loops=%u delay=%u timing_millihz=%u checksum=%016llx\n",
           PLAYER_MARKER,c.anbf_version,c.codec==ANBF_CODEC_JPEG?"JPEG":"QOI",c.width,c.height,c.frame_count,c.loops,c.delay_ms,c.timing_millihz,(unsigned long long)checksum);
    free(buf); cleanup(&c,1); return 0;
}



static int jcx_pipeline_selftest(const char *path) {
    struct player_ctx c; memset(&c,0,sizeof(c)); c.fb_fd=-1;c.tty_fd=-1;
    int rc=parse_anbf_jcx(&c,path); if(rc){cleanup(&c,1);return 43;}
    c.pf.width=c.width;c.pf.height=c.height;c.pf.bytespp=4;c.pf.fast_bgra32=1;
    c.slot_bytes=(size_t)c.width*c.height*4u;
    c.vinfo.xres=c.width;c.vinfo.yres=c.height;c.vinfo.xres_virtual=c.width;c.vinfo.yres_virtual=c.height;
    c.render_width=c.width;c.render_height=c.height;c.vinfo.bits_per_pixel=32;c.finfo.line_length=c.width*4u;
    c.fb_map_size=(size_t)c.finfo.line_length*c.height;
    c.fb_map=mmap(NULL,c.fb_map_size,PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0);
    if(c.fb_map==MAP_FAILED){c.fb_map=NULL;cleanup(&c,1);return 44;}
    if((rc=configure_pool(&c))!=0){cleanup(&c,1);return 45;}
    if((rc=run_pipeline(&c))!=0){fprintf(stderr,"JCXPIPE rc=%d err=%d\\n",rc,c.error);cleanup(&c,1);return 46;}
    printf("%s JCXPIPE OK pool=%d buffers=%d decoded=%llu shown=%llu direct=%llu chunks=%u xz_tasks=%llu jpeg_tasks=%llu max_active=%d/%d switches=%llu\\n",PLAYER_MARKER,c.worker_count,c.slot_count,(unsigned long long)c.decoded_frames,(unsigned long long)c.displayed_frames,(unsigned long long)c.direct_presents,c.jcx_chunk_count,(unsigned long long)c.jcx_pool_xz_tasks,(unsigned long long)c.jcx_pool_decode_tasks,c.jcx_max_active_xz,c.jcx_max_active_decode,(unsigned long long)c.jcx_pool_role_switches);
    cleanup(&c,1);return 0;
}





static int pipeline_selftest_anbf(const char *path) {
    struct player_ctx c; memset(&c,0,sizeof(c)); c.fb_fd=-1; c.tty_fd=-1;
    int rc=parse_anbf(&c,path);
    if (rc) { fprintf(stderr,"PIPESELFTEST parse rc=%d\n",rc); cleanup(&c,1); return 5; }
    c.pf.width=c.width; c.pf.height=c.height; c.pf.bytespp=4; c.pf.fast_bgra32=1;
    c.slot_bytes=(size_t)c.width*c.height*4u;
    c.vinfo.xres=c.width; c.vinfo.yres=c.height; c.vinfo.xres_virtual=c.width; c.vinfo.yres_virtual=c.height;
    c.render_width=c.width; c.render_height=c.height; c.scale_down=0;
    c.vinfo.bits_per_pixel=32; c.finfo.line_length=c.width*4u;
    c.fb_map_size=(size_t)c.finfo.line_length*c.height;
    c.fb_map=mmap(NULL,c.fb_map_size,PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0);
    if (c.fb_map==MAP_FAILED) { c.fb_map=NULL; cleanup(&c,1); return 6; }
    rc=configure_pool(&c);
    if (rc) { fprintf(stderr,"PIPESELFTEST pool rc=%d\n",rc); cleanup(&c,1); return 7; }
    rc=run_pipeline(&c);
    if (rc) { fprintf(stderr,"PIPESELFTEST pipeline rc=%d\n",rc); cleanup(&c,1); return 8; }
    printf("%s PIPESELFTEST OK workers=%d buffers=%d decoded=%llu shown=%llu direct=%llu\n",
           PLAYER_MARKER,c.worker_count,c.slot_count,(unsigned long long)c.decoded_frames,
           (unsigned long long)c.displayed_frames,(unsigned long long)c.direct_presents);
    cleanup(&c,1); return 0;
}

int main(int argc,char **argv) {
    if (argc==3 && !strcmp(argv[1],"--selftest")) return selftest_anbf(argv[2]);
    if (argc==3 && !strcmp(argv[1],"--pipeline-selftest")) return pipeline_selftest_anbf(argv[2]);
    if (argc==3 && !strcmp(argv[1],"--jcx-pipeline-selftest")) return jcx_pipeline_selftest(argv[2]);
    struct player_ctx c; memset(&c,0,sizeof(c)); c.fb_fd=-1; c.tty_fd=-1;
    c.boot_player_start_ns=boot_ns();
    int rc;
    if (access(g_anim_jcx_path,R_OK)==0) rc=parse_anbf_jcx(&c,g_anim_jcx_path);
    else rc=parse_anbf(&c,g_anim_path);
    c.boot_anbf_ready_ns=boot_ns();
    if (!rc && !c.jcx_mode && c.codec!=ANBF_CODEC_QOI) rc=-24;
    if (rc) { fprintf(stderr,"AnimeBoot Neo Debug: current animation parse failed rc=%d\n",rc); cleanup(&c,1); return 20; }
    /* Runtime override has strict priority over the ANBF header.  This makes
       FPS tuning a grub.cfg-only operation: no QOI re-encode or initramfs
       rebuild is needed.  When absent, the precise ANBF milli-Hz rate is used. */
    int runtime_fps = read_cmdline_int("AnimeBootNeoPlayerFPS", 0, 0, 1000);
    c.timing_mode = read_cmdline_int("AnimeBootNeoDebug", 0, 0, 1);
    if (runtime_fps > 0) {
        c.timing_millihz = (uint32_t)runtime_fps * 1000u;
        c.period_ns = NS_PER_SEC / (uint64_t)runtime_fps;
        if (!c.period_ns) c.period_ns = 1;
    }
    rc=open_framebuffer(&c);
    c.boot_fb_ready_ns=boot_ns();
    if (rc) { fprintf(stderr,"AnimeBoot Neo Debug: framebuffer init failed rc=%d\n",rc); cleanup(&c,1); return 21; }
    rc=configure_pool(&c);
    c.boot_pool_ready_ns=boot_ns();
    if (rc) { fprintf(stderr,"AnimeBoot Neo Debug: buffer pool failed rc=%d\n",rc); cleanup(&c,1); return 22; }
    fprintf(stderr,"%s ANBF262 v=%u codec=%s %ux%u frames=%u loops=%u source=%.3fFPS override=%d cpu=%ld workers=%d buffers=%d cache=%.1fMiB fb=%ux%u-%ubpp stride=%u timing_est=%.3fHz direct_no_drop=1\n",
            PLAYER_MARKER,c.anbf_version,c.codec==ANBF_CODEC_JPEG?"JPEG":"QOI",c.width,c.height,c.frame_count,c.loops,1000000000.0/(double)c.period_ns,runtime_fps,
            sysconf(_SC_NPROCESSORS_ONLN),c.worker_count,c.slot_count,
            (double)c.slot_bytes*c.slot_count/(1024.0*1024.0),c.vinfo.xres,c.vinfo.yres,c.vinfo.bits_per_pixel,c.finfo.line_length,
            c.refresh_millihz/1000.0);
    fprintf(stderr,"AnimeBoot Neo Debug: source_mode=%s compressed_path=%s\n",
            c.jcx_mode?"JPEG_CHUNKED_XZ32_DYNAMIC":"QOI_RAW_MMAP",
            c.jcx_mode?g_anim_jcx_path:g_anim_path);
    fprintf(stderr,"AnimeBoot Neo Debug: fb id=%s virtual=%ux%u xpanstep=%u ypanstep=%u ywrapstep=%u boot_timing=%d\n",
            c.finfo.id,c.vinfo.xres_virtual,c.vinfo.yres_virtual,c.finfo.xpanstep,c.finfo.ypanstep,c.finfo.ywrapstep,c.timing_mode);
    if (c.scale_down) fprintf(stderr,"AnimeBoot Neo Debug: render=%ux%u scaled from %ux%u\n",c.render_width,c.render_height,c.width,c.height);
    rc=run_pipeline(&c);
    fprintf(stderr,"AnimeBoot Neo Debug: summary rc=%d decoded=%llu shown=%llu direct=%llu max_present=%.3fms\n",
            rc,(unsigned long long)c.decoded_frames,(unsigned long long)c.displayed_frames,
            (unsigned long long)c.direct_presents,(double)c.max_present_ns/1000000.0);
    timing_framebuffer_pause(&c,rc);
    if (rc) { cleanup(&c,1); return 23; }
    /* Keep KD_GRAPHICS and the final framebuffer image visible. The PID1 wrapper
       immediately performs the already-established UEFI/Legacy handoff. */
    cleanup(&c,0);
    return 0;
}

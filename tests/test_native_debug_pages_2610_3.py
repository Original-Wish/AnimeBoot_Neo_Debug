# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
# -*- coding: utf-8 -*-
"""Run the actual native debug glyph renderer on a 640x480 surface.

The Windows CLI harness includes the exact header used by the Linux player.
VM tests additionally confirm both pages stay visible in the real GOP/DRM
surface: receiving the page text on a serial console is insufficient.
"""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest
from PIL import Image

HARNESS = r'''
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "animeboot_debug_font.h"
struct surface {unsigned char *pixels;int page;};
static void pixel(void *opaque,uint32_t x,uint32_t y,int heading){
    struct surface *s=opaque;
    unsigned char *out=s->pixels+(y*640+x)*3;
    out[0]=heading?(s->page==1?60:255):230;
    out[1]=heading?(s->page==1?215:195):238;
    out[2]=heading?(s->page==1?255:45):255;
}
int main(int argc,char **argv){
    if(argc!=3)return 2;
    int page=atoi(argv[2]);size_t bytes=640*480*3;
    unsigned char *memory=malloc(bytes+64);if(!memory)return 3;
    memset(memory,0xCC,bytes+64);memset(memory+32,0,bytes);
    struct surface s={memory+32,page};
    const char *text=page==1?
        "AnimeBoot Neo Debug (2610.3) - STARTUP [1/2]\n\n"
        "Result: OK   codec: JPEG Chunk32 dynamic\n"
        "Framebuffer: DRM dumb  640x480  32 bpp  stride=2560\n"
        "CPU=4 workers=4 buffers=8 cache=9.4 MiB\n\n"
        "Kernel -> player entry : 123.456 ms\n"
        "Read animation header  : 1.000 ms\n"
        "Initialize framebuffer : 2.000 ms\n"
        "Allocate buffer pool   : 3.000 ms\n"
        "Pipeline -> first frame: 4.000 ms\n"
        "Kernel -> first frame  : 130.456 ms\n\n"
        "Kernel clock excludes firmware/GRUB file loading.\n"
        "No Linux disk discovery or writes are required.\n"
        "Next page in 8 seconds.\n":
        "AnimeBoot Neo Debug (2610.3) - PLAYBACK [2/2]\n\n"
        "Target 60.000 FPS frames=120 loops=1\n"
        "Playback elapsed: 2000.000 ms effective: 60.000 FPS\n"
        "Decoded / shown: 120 / 120\n"
        "Late frames (>1 ms): 0 max lateness: 0.000 ms\n"
        "Max presentation gap: 16.700 ms max copy: 0.100 ms\n"
        "Gaps >33 / >50 / >100 ms: 0 / 0 / 0\n\n"
        "Display wait total: 1.000 ms (XZ 0.500 / decode 0.500)\n"
        "Decode total across workers: 20.000 ms max one: 1.000 ms\n"
        "Direct framebuffer, no deliberate frame dropping.\n"
        "Debug display time is excluded from playback FPS.\n\n"
        "Reboot to Windows in 8 seconds. Esc skip is not active here.\n";
    neo_debug_draw_text(&s,640,480,text,pixel);
    for(int i=0;i<32;++i)if(memory[i]!=0xCC||memory[bytes+32+i]!=0xCC)return 4;
    FILE *f=fopen(argv[1],"wb");if(!f)return 5;
    fprintf(f,"P6\n640 480\n255\n");fwrite(memory+32,1,bytes,f);fclose(f);free(memory);return 0;
}
'''

TIMING_SHIM = r'''
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include "animeboot_debug_font.h"
#define NS_PER_SEC 1000000000ull
#define _SC_NPROCESSORS_ONLN 1
struct player_ctx {
 int timing_mode,jcx_mode,worker_count,slot_count;
 struct {char id[16];uint32_t line_length;} finfo;
 struct {uint32_t xres,yres,bits_per_pixel;} vinfo;
 uint64_t slot_bytes,period_ns,displayed_frames,decoded_frames;
 uint32_t frame_count,loops;
 uint64_t boot_pipeline_end_ns,boot_first_present_ns,boot_player_start_ns;
 uint64_t boot_anbf_ready_ns,boot_fb_ready_ns,boot_pool_ready_ns,boot_pipeline_start_ns;
 uint64_t late_frame_count,late_max_ns,present_gap_max_ns,max_present_ns;
 uint64_t present_gap_over_33ms,present_gap_over_50ms,present_gap_over_100ms;
 uint64_t display_wait_total_ns,display_wait_xz_ns,display_wait_decode_ns;
 uint64_t decode_total_ns,decode_max_ns;
 unsigned char *fb_map;
};
static long sysconf(int ignored){(void)ignored;return 4;}
static uint64_t clock_now;
static int draws,sleeps,restores,bad;
static const char *output_dir;
static unsigned char *surface;
static uint64_t mono_ns(void){return clock_now;}
static double ms_between(uint64_t a,uint64_t b){return a>=b?(double)(a-b)/1e6:0;}
static double ms_since_boot(uint64_t a){return (double)a/1e6;}
static void neo_drm_restore(struct player_ctx *ctx){++restores;ctx->fb_map=NULL;}
static void sleep_until_ns(uint64_t target){
 if(target-clock_now!=8*NS_PER_SEC || draws!=sleeps+1 || restores)bad=1;
 ++sleeps;clock_now=target;
}
struct surface {unsigned char *pixels;int page;};
static void pixel(void *opaque,uint32_t x,uint32_t y,int heading){
 struct surface *s=opaque;unsigned char *out=s->pixels+(y*640+x)*3;
 out[0]=heading?(s->page==1?60:255):230;
 out[1]=heading?(s->page==1?215:195):238;
 out[2]=heading?(s->page==1?255:45):255;
}
static void debug_draw_page(struct player_ctx *ctx,const char *text,int page){
 ++draws;
 if(ctx->fb_map!=surface || restores || page!=draws ||
    !strstr(text,"AnimeBoot Neo Debug (2610.3)") ||
    !strstr(text,page==1?"STARTUP [1/2]":"PLAYBACK [2/2]"))bad=1;
 memset(surface,0,640*480*3);struct surface s={surface,page};
 neo_debug_draw_text(&s,640,480,text,pixel);
 char path[1024];snprintf(path,sizeof(path),"%s/timing%d.ppm",output_dir,page);
 FILE *f=fopen(path,"wb");if(!f){bad=1;return;}
 fprintf(f,"P6\n640 480\n255\n");fwrite(surface,1,640*480*3,f);fclose(f);
}
/* ACTUAL_PRODUCTION_TIMING_FUNCTION */
int main(int argc,char **argv){
 if(argc!=2)return 2;output_dir=argv[1];surface=calloc(640*480*3,1);if(!surface)return 3;
 struct player_ctx ctx={0};ctx.timing_mode=1;ctx.jcx_mode=1;ctx.fb_map=surface;
 strcpy(ctx.finfo.id,"DRM dumb");ctx.finfo.line_length=2560;
 ctx.vinfo.xres=640;ctx.vinfo.yres=480;ctx.vinfo.bits_per_pixel=32;
 ctx.worker_count=4;ctx.slot_count=8;ctx.slot_bytes=1228800;
 ctx.period_ns=16666667;ctx.frame_count=120;ctx.loops=1;
 ctx.displayed_frames=ctx.decoded_frames=120;
 ctx.boot_player_start_ns=100000000;ctx.boot_anbf_ready_ns=101000000;
 ctx.boot_fb_ready_ns=103000000;ctx.boot_pool_ready_ns=106000000;
 ctx.boot_pipeline_start_ns=107000000;ctx.boot_first_present_ns=108000000;
 ctx.boot_pipeline_end_ns=2108000000;
 timing_framebuffer_pause(&ctx,0);
 if(draws!=2||sleeps!=2||restores||clock_now!=16*NS_PER_SEC||ctx.fb_map!=surface)bad=1;
 free(surface);return bad?9:0;
}
'''

class NativeDebugDrawingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root=Path(__file__).resolve().parents[1]
        toolchain=cls.root.parent/'toolchain'
        zig=toolchain/'zig/zig-windows-x86_64-0.13.0/zig.exe'
        if not zig.is_file():raise unittest.SkipTest('Native Zig toolchain unavailable')
        cls.temp=tempfile.TemporaryDirectory(prefix='native_debug_glyphs_')
        cls.directory=Path(cls.temp.name)
        source=cls.directory/'draw.c';source.write_text(HARNESS,encoding='utf-8')
        cls.exe=cls.directory/'draw.exe'
        env=os.environ.copy();env['ZIG_GLOBAL_CACHE_DIR']=str(toolchain/'zig-cache')
        subprocess.run([str(zig),'cc','-target','x86_64-windows-gnu','-O2','-static',
                        '-I'+str(cls.root/'assets/linux'),str(source),'-o',str(cls.exe)],env=env,check=True)
        player=(cls.root/'assets/linux/animeboot_player.c').read_text(encoding='utf-8')
        prefix='static void timing_framebuffer_pause('
        body=prefix+player.split(prefix,1)[1].split('\n}\n',1)[0]+'\n}\n'
        timing_source=cls.directory/'timing.c'
        timing_source.write_text(TIMING_SHIM.replace('/* ACTUAL_PRODUCTION_TIMING_FUNCTION */',body),encoding='utf-8')
        cls.timing_exe=cls.directory/'timing.exe'
        subprocess.run([str(zig),'cc','-target','x86_64-windows-gnu','-O2','-static',
                        '-I'+str(cls.root/'assets/linux'),str(timing_source),'-o',str(cls.timing_exe)],env=env,check=True)

    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()

    def _page(self,page):
        path=self.directory/('page%d.ppm'%page)
        subprocess.run([str(self.exe),str(path),str(page)],check=True)
        with Image.open(path) as image:return image.convert('RGB')

    def test_startup_page_has_readable_native_glyphs(self):
        image=self._page(1)
        self.assertEqual(image.size,(640,480))
        self.assertEqual(image.getpixel((17,16)),(60,215,255))
        colors=dict((color,count) for count,color in image.getcolors(640*480))
        self.assertGreater(colors.get((60,215,255),0),300)
        self.assertGreater(colors.get((230,238,255),0),3000)
        self.assertEqual(image.getbbox()[0],16)

    def test_playback_page_is_distinct_and_complete_at_vga_size(self):
        first=self._page(1);second=self._page(2)
        self.assertNotEqual(first.tobytes(),second.tobytes())
        self.assertEqual(second.getpixel((17,16)),(255,195,45))
        self.assertLess(second.getbbox()[2],640)
        self.assertLess(second.getbbox()[3],480)
        self.assertGreater(second.crop((16,220,640,320)).getbbox()[3],0)

    def test_actual_player_two_pages_each_hold_eight_seconds_on_same_surface(self):
        subprocess.run([str(self.timing_exe),str(self.directory)],check=True,capture_output=True)
        with Image.open(self.directory/'timing1.ppm') as first, \
             Image.open(self.directory/'timing2.ppm') as second:
            self.assertEqual(first.getpixel((17,16)),(60,215,255))
            self.assertEqual(second.getpixel((17,16)),(255,195,45))
            self.assertNotEqual(first.tobytes(),second.tobytes())

if __name__=='__main__':unittest.main()

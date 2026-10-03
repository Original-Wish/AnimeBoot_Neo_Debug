/* Minimal dumb-buffer backend for Fedora kernels with CONFIG_FB_DEVICE=n.
 * Uses the kernel's existing display mode and simpledrm; no GPU modules,
 * acceleration, vblank waits, page flipping or new frame scheduling policy.
 * Included after player_ctx; DRM UAPI headers are supplied by the toolchain.
 */
static int neo_drm_open(struct player_ctx *ctx) {
    for (int card=0;card<16;++card) {
        char path[64];snprintf(path,sizeof(path),"/dev/dri/card%d",card);
        int fd=open(path,O_RDWR|O_CLOEXEC);
        if (fd<0) { if(card==0)fprintf(stderr,"AnimeBoot Neo Debug: %s open: %s\n",path,strerror(errno));continue; }
        struct drm_mode_card_res res={0};
        uint32_t connectors[32]={0},crtcs[32]={0};
        if (ioctl(fd,DRM_IOCTL_MODE_GETRESOURCES,&res)<0 || res.count_connectors>32 || res.count_crtcs>32) { fprintf(stderr,"DRM resources: %s\n",strerror(errno));close(fd);continue; }
        res.connector_id_ptr=(uintptr_t)connectors;res.crtc_id_ptr=(uintptr_t)crtcs;
        /* The second ioctl must not advertise capacity for arrays whose
         * pointers are NULL. GETRESOURCES otherwise copies FB/encoder IDs to
         * address zero and fails EFAULT whenever fbcon has created a buffer.
         */
        res.count_fbs=0;res.count_encoders=0;
        if(ioctl(fd,DRM_IOCTL_MODE_GETRESOURCES,&res)<0 || res.count_connectors>32 || res.count_crtcs>32){fprintf(stderr,"DRM resource IDs: %s\n",strerror(errno));close(fd);continue;}
        fprintf(stderr,"AnimeBoot Neo Debug: %s resources connectors=%u crtcs=%u encoders=%u\n",path,res.count_connectors,res.count_crtcs,res.count_encoders);
        for (uint32_t n=0;n<res.count_connectors;++n) {
            struct drm_mode_get_connector con={0};con.connector_id=connectors[n];
            if(ioctl(fd,DRM_IOCTL_MODE_GETCONNECTOR,&con)<0 || con.connection!=1 || !con.encoder_id){fprintf(stderr,"DRM connector state=%u encoder=%u modes=%u\n",con.connection,con.encoder_id,con.count_modes);continue;}
            struct drm_mode_get_encoder enc={0};enc.encoder_id=con.encoder_id;
            if(ioctl(fd,DRM_IOCTL_MODE_GETENCODER,&enc)<0 || !enc.crtc_id){fprintf(stderr,"DRM encoder crtc=%u: %s\n",enc.crtc_id,strerror(errno));continue;}
            struct drm_mode_crtc saved={0};saved.crtc_id=enc.crtc_id;
            if(ioctl(fd,DRM_IOCTL_MODE_GETCRTC,&saved)<0 || !saved.mode_valid){fprintf(stderr,"DRM CRTC mode_valid=%u: %s\n",saved.mode_valid,strerror(errno));continue;}
            uint32_t width=saved.mode.hdisplay,height=saved.mode.vdisplay;
            if(!width || !height || width>16384 || height>16384)continue;
            struct drm_mode_create_dumb dumb={0};dumb.width=width;dumb.height=height;dumb.bpp=32;
            if(ioctl(fd,DRM_IOCTL_MODE_CREATE_DUMB,&dumb)<0){fprintf(stderr,"DRM create dumb: %s\n",strerror(errno));continue;}
            struct drm_mode_fb_cmd fb={0};fb.width=width;fb.height=height;fb.pitch=dumb.pitch;fb.bpp=32;fb.depth=24;fb.handle=dumb.handle;
            struct drm_mode_map_dumb map={0};map.handle=dumb.handle;
            void *pixels=MAP_FAILED;
            if(dumb.size && dumb.size<=SIZE_MAX && dumb.pitch>=width*4 && dumb.size>=(uint64_t)dumb.pitch*height &&
               ioctl(fd,DRM_IOCTL_MODE_ADDFB,&fb)==0 && ioctl(fd,DRM_IOCTL_MODE_MAP_DUMB,&map)==0) {
                pixels=mmap(NULL,(size_t)dumb.size,PROT_READ|PROT_WRITE,MAP_SHARED,fd,(off_t)map.offset);
            }
            if(pixels==MAP_FAILED)fprintf(stderr,"DRM add/map framebuffer: %s\n",strerror(errno));
            if(pixels!=MAP_FAILED) {
                memset(pixels,0,(size_t)dumb.size);
                struct drm_mode_crtc active=saved;
                active.fb_id=fb.fb_id;active.x=active.y=0;
                active.set_connectors_ptr=(uintptr_t)&con.connector_id;active.count_connectors=1;
                if(ioctl(fd,DRM_IOCTL_MODE_SETCRTC,&active)==0) {
                    ctx->drm_mode=1;ctx->drm_handle=dumb.handle;ctx->drm_fb_id=fb.fb_id;
                    ctx->drm_connector_id=con.connector_id;ctx->drm_saved=saved;
                    ctx->fb_fd=fd;ctx->fb_map=pixels;ctx->fb_map_size=(size_t)dumb.size;
                    memset(&ctx->finfo,0,sizeof(ctx->finfo));memset(&ctx->vinfo,0,sizeof(ctx->vinfo));
                    snprintf(ctx->finfo.id,sizeof(ctx->finfo.id),"DRM dumb");
                    ctx->finfo.line_length=dumb.pitch;ctx->finfo.smem_len=(uint32_t)dumb.size;
                    ctx->vinfo.xres=ctx->vinfo.xres_virtual=width;ctx->vinfo.yres=ctx->vinfo.yres_virtual=height;
                    ctx->vinfo.bits_per_pixel=32;
                    ctx->vinfo.red.offset=16;ctx->vinfo.red.length=8;
                    ctx->vinfo.green.offset=8;ctx->vinfo.green.length=8;
                    ctx->vinfo.blue.offset=0;ctx->vinfo.blue.length=8;
                    fprintf(stderr,"AnimeBoot Neo Debug: using %s dumb buffer %ux%u (fbdev unavailable)\n",path,width,height);
                    return 0;
                }
                fprintf(stderr,"DRM set CRTC: %s\n",strerror(errno));
                munmap(pixels,(size_t)dumb.size);
            }
            if(fb.fb_id)ioctl(fd,DRM_IOCTL_MODE_RMFB,&fb.fb_id);
            struct drm_mode_destroy_dumb destroy={0};destroy.handle=dumb.handle;
            ioctl(fd,DRM_IOCTL_MODE_DESTROY_DUMB,&destroy);
        }
        close(fd);
    }
    return -1;
}

static void neo_drm_flush(struct player_ctx *ctx) {
    if(!ctx->drm_mode || !ctx->drm_fb_id)return;
    struct drm_mode_fb_dirty_cmd dirty={0};dirty.fb_id=ctx->drm_fb_id;
    /* simpledrm copies the updated dumb-buffer content to firmware memory. */
    (void)ioctl(ctx->fb_fd,DRM_IOCTL_MODE_DIRTYFB,&dirty);
}

static void neo_drm_restore(struct player_ctx *ctx) {
    if(!ctx->drm_mode || !ctx->drm_fb_id)return;
    struct drm_mode_crtc saved=ctx->drm_saved;
    saved.set_connectors_ptr=(uintptr_t)&ctx->drm_connector_id;saved.count_connectors=1;
    if(ioctl(ctx->fb_fd,DRM_IOCTL_MODE_SETCRTC,&saved)<0)
        fprintf(stderr,"AnimeBoot Neo Debug: console restore failed: %s\n",strerror(errno));
    (void)ioctl(ctx->fb_fd,DRM_IOCTL_MODE_RMFB,&ctx->drm_fb_id);
    ctx->drm_fb_id=0;
    (void)ioctl(ctx->fb_fd,DRM_IOCTL_DROP_MASTER,0);
}

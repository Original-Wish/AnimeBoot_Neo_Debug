/* SPDX-License-Identifier: GPL-3.0-or-later
 * AnimeBoot Neo: preserve the selected VBE resolution across Fedora's BIOS
 * real-mode Linux loader. No disk access, vendor code, or EFI modification.
 */
#define GRUB_FILE "neo_vbe.c"
#include <grub/dl.h>
#include <grub/command.h>
#include <grub/env.h>
#include <grub/misc.h>
#include <grub/video.h>
#include <grub/i386/pc/int.h>
GRUB_MOD_LICENSE("GPLv3+");
GRUB_MOD_NAME(neo_vbe);
static grub_command_t command;
static grub_err_t neo_mode(grub_command_t cmd __attribute__((unused)),
                           int argc __attribute__((unused)),
                           char **argv __attribute__((unused))) {
    struct grub_bios_int_registers regs;
    grub_uint32_t mode;
    char value[32];
    grub_env_unset("neo_video_args");
    if (grub_video_get_driver_id()!=GRUB_VIDEO_DRIVER_VBE)
        return grub_error(GRUB_ERR_BAD_DEVICE,"AnimeBoot Neo: no active VBE framebuffer");
    grub_memset(&regs,0,sizeof(regs));regs.eax=0x4f03;
    grub_bios_interrupt(0x10,&regs);
    mode=regs.ebx&0x3fff;
    if ((regs.eax&0xffff)!=0x004f || mode<0x100 || mode>=0x200)
        return grub_error(GRUB_ERR_BAD_DEVICE,"AnimeBoot Neo: cannot read current VBE mode");
    /* Linux real-mode video IDs use VBE ID + 0x200; do not guess a mode from
     * project dimensions. The same firmware mode will be re-selected by setup.
     */
    grub_snprintf(value,sizeof(value),"vga=0x%x",mode+0x200);
    return grub_env_set("neo_video_args",value);
}
GRUB_MOD_INIT(neo_vbe) {
    command=grub_register_command("neo_vbe_payload",neo_mode,0,
                                "Pass the active VBE mode to Fedora BIOS Linux.");
}
GRUB_MOD_FINI(neo_vbe) { grub_unregister_command(command); }

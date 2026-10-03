# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
"""Current boot configuration: firmware-bound EFI and filesystem-bound BIOS."""

# 本模块生成文本配置，不修改 Fedora 已签名 EFI 或内核。UEFI/BIOS 共享静图、动画选择和 one-shot 接力语义。
# 第一步消费 neo_once：值为1时清零并直接转 Windows，必须早于画布/动画轮换，否则动画重启会推进两次。
# 普通开机显示当前静图；动画启用时写 neo_once=1，再启动 RAM 中的 Linux/播放器。播放器结束重启，由下一次 GRUB 消费标记。
# 无动图 + 无感融入 + 所有画布倒计时均为 none，才采用外层 sleep/normal_exit 分支；任何可见控件都使用菜单倒计时。
# 静图转 Linux 始终黑屏交接；Esc 返回外层直接执行 Windows，不把“自然结束的黑屏动作”加到 Esc 分支。
# 环境块是固定1024字节 GRUB 文件，由 GRUB 读写；不位于 MBR、LBA1，也不由 Linux 写磁盘。

import re
from .resolution import gfxmode_value
from .constants import THEME_ROOT
from .scenes import countdown_modes, static_selection_script, theme_paths

ENV_PATH = 'AnimeBootNeo/bootstate.env'
BIOS_CONFIG = 'AnimeBootNeo/grub.cfg'
UEFI_CONFIG = 'EFI/fedora/grub.cfg'


def static_handoff_strategy(project):
    """Fix the handoff strategy at build time, using all three UI choices."""
    if (not project.get('boot_animation', {}).get('enabled')
            and project.get('static_windows_transition', 'blend') == 'blend'
            and all(mode == 'none' for mode in countdown_modes(project))
            and int(project.get('timeout', 3)) > 0):
        return 'external_timer'
    return 'menu_timer'


def validate_boot_binding(binding):
    if not isinstance(binding, dict):
        raise ValueError('Legacy installation requires the target filesystem identity')
    filesystem = binding.get('filesystem', '').upper()
    value = binding.get('fs_uuid', '').upper()
    pattern = r'[0-9A-F]{16}' if filesystem == 'NTFS' else r'[0-9A-F]{4}-[0-9A-F]{4}'
    if filesystem not in ('NTFS', 'FAT16', 'FAT32') or not re.fullmatch(pattern, value):
        raise ValueError('Invalid target filesystem identity')
    return {'filesystem': filesystem, 'fs_uuid': value}


def create_blank_grubenv(armed=False):
    text = (b'# GRUB Environment Block\n' + (b'neo_once=1\n' if armed else b'neo_once=0\n')
            + b'neo_rotation=0\nneo_static_rotation=0\n')
    return text + b'#' * (1024-len(text))


def valid_grubenv(data):
    if len(data)!=1024 or not data.startswith(b'# GRUB Environment Block\n'):return False
    content=data[len(b'# GRUB Environment Block\n'):]
    body,separator,padding=content.partition(b'#')
    if separator and padding.strip(b'#'):return False
    try:
        lines=body.decode('ascii').splitlines();values={}
        for line in lines:
            name,value=line.split('=',1)
            if name in values:return False
            values[name]=value
    except (ValueError,UnicodeError):return False
    return (set(values)=={'neo_once','neo_rotation','neo_static_rotation'} and values['neo_once'] in ('0','1')
            and all(values[key] in tuple(str(n) for n in range(8))
                    for key in ('neo_rotation','neo_static_rotation')))


def embedded_config(boot_binding=None, fix_gfxterm_blank=True, normal_menu=False):
    # lnxboot's Linux entry defaults to BIOS disk 0x80. Bind to the filesystem
    # selected by the installer rather than assuming hd0 or finding a filename.
    first='echo ""\n' if fix_gfxterm_blank else ''
    if boot_binding is None:
        return (first+"echo 'AnimeBoot Neo: install this payload to bind its BIOS boot volume'\nhalt\n").encode('ascii')
    binding = validate_boot_binding(boot_binding)
    command='normal' if normal_menu else 'configfile'
    return (first+"search --no-floppy --fs-uuid --set=root " + binding['fs_uuid'] +
            "\n" + command + " ($root)/" + BIOS_CONFIG +
            "\nset color_normal=light-gray/black\nntldr ($root)/AnimeBootNeo/windows.bootmgr\nboot\n").encode('ascii')


# 根据不可变制作参数一次性选择策略。生成的GRUB代码要同时处理首次开机、Esc、动图重启接力；不要在菜单内期待并行Python式定时器。
def generate_config(project, platform='bios'):
    if platform not in ('bios', 'uefi'):
        raise ValueError('Unsupported current boot platform')
    anim = bool(project.get('boot_animation', {}).get('enabled'))
    from .rotation import initramfs_paths,rotation_script
    rotating=len(initramfs_paths(project))>1
    static_rotating=len(theme_paths(project))>1
    debug = bool(project.get('show_debug_info', True))
    fps = int(project.get('boot_animation', {}).get('frame_rate', 60))
    timeout = int(project.get('timeout', 3))
    handoff = static_handoff_strategy(project)
    transition='black' if anim else project.get('static_windows_transition','blend')
    if transition not in ('blend','black'):raise ValueError('Unsupported static Windows transition')
    # Kernel diagnostics stay quiet in both modes. The checkbox controls the
    # player's own diagnostic pages, never kernel/shim/GRUB startup banners.
    args = ('quiet loglevel=0 fbcon=nodefer AnimeBootNeoDebug=%d' % int(debug))
    args += (' logo.nologo vt.global_cursor_default=0 consoleblank=0'
             ' AnimeBootNeoMode=WindowsAnimation AnimeBootNeoHandoff=Armed'
             ' AnimeBootNeoPlayerFPS=%d' % fps)
    s = ['# AnimeBoot Neo Debug (2610.3)']
    if project.get('fix_gfxterm_blank', True):
        s.append('echo ""')
    s += ['set pager=0','set neo_static_transition='+transition,
          'set neo_static_handoff='+handoff]
    # GRUB's grammar only permits function definitions at top level. Define
    # this before the EFI phase wrapper; a function inside its else block is
    # rejected by both grub-script-check and the signed firmware interpreter.
    s += ['function neo_black {', '  set theme=', '  set color_normal=black/black',
          '  set color_highlight=black/black']
    if platform == 'bios':
        s += ['  terminal_output console',
              '  set gfxmode=' + gfxmode_value(*project['resolution']),
              '  terminal_output gfxterm']
    # EFI must retain its active GOP mode. Switching through the firmware text
    # console can invalidate the framebuffer geometry handed to the kernel.
    s += ['  clear', '  set gfxpayload=keep', '  set color_normal=light-gray/black', '}']
    nested_menu = platform == 'uefi' or not anim
    if platform == 'uefi':
        # Fedora's signed EFI builds export fw_path (cmdpath is unset).
        # It contains the firmware-selected GRUB device and executable path.
        s += ["if regexp --set=1:neo_root '^\\(([^)]+)\\)' \"$fw_path\"; then",
              '  set root=$neo_root', 'else',
              "  echo 'AnimeBoot Neo: EFI boot device is unavailable'", '  halt', 'fi']
    else:
        # The embedded BIOS configuration has already bound root by UUID.
        s.append('set neo_root=$root')
    s += ['function neo_windows {', '  set color_normal=light-gray/black']
    if platform == 'uefi':
        s.append('  chainloader ($neo_root)/EFI/Microsoft/Boot/bootmgfw.efi_ANBAK')
    else:
        s.append('  ntldr ($neo_root)/AnimeBootNeo/windows.bootmgr')
    s += ['  boot', '}']
    if anim or static_rotating:
        s += ['set neo_env=($neo_root)/' + ENV_PATH]
    if anim:
        s += [
              'function neo_clear {', '  set neo_once=0', '  save_env -f $neo_env neo_once', '}',
              'function neo_arm {', '  set neo_once=1', '  save_env -f $neo_env neo_once', '}',
              'set neo_once=invalid', 'if load_env -f $neo_env neo_once; then',
              '  if [ $neo_once = 1 ]; then', '    neo_clear', '    neo_windows', '  fi',
              'else', '  neo_windows', 'fi', 'if [ $neo_once != 0 ]; then', '  neo_windows', 'fi']
    if rotating:
        s += ['load_env -f $neo_env neo_rotation'] + rotation_script(project)
    if nested_menu:
        # One physical configuration, two interpreter contexts. GRUB's Esc
        # exits the nested configfile without running the timed menu entry.
        # Export the phase so the nested evaluation creates the menu instead
        # of nesting again. Returning here boots Windows without neo_black.
        config_path=UEFI_CONFIG if platform=='uefi' else BIOS_CONFIG
        s += ['if [ "$neo_menu_phase" != active ]; then',
              '  set neo_menu_phase=active', '  export neo_menu_phase', '  export fw_path',
              '  configfile ($neo_root)/' + config_path,
              '  set neo_menu_phase=']
        if handoff == 'external_timer':
            # The nested menu has already drawn the theme and returned before
            # the timeout-entry clear. Sleep preserves the framebuffer, accepts
            # Esc, and never starts Linux. The theme has no timeout component.
            s.append('  sleep --interruptible %d' % timeout)
        s += ['  neo_windows', 'else']
    if platform == 'bios':
        s += ['insmod vbe', 'insmod neo_vbe',
              'loadfont ($neo_root)/AnimeBootNeo/unicode.pf2']
    else:
        # Native PCI video drivers can change scanout without updating the GOP
        # mode read by the signed Linux EFI stub. First release the fake
        # all_video aggregator's dependency references, then remove only the
        # PCI adapters. Built-in GOP/UGA retain their own references. No module
        # is reloaded and no external .mod file is read under Secure Boot.
        s += ['rmmod all_video', 'rmmod video_bochs', 'rmmod video_cirrus']
    # The signed Fedora EFI image already contains these drivers/modules.
    # Never ask Secure Boot to reload an unsigned external .mod.
    # External PF2 loading is forbidden by its shim verifier. Canvas text is
    # baked into the background PNG; GRUB's built-in ASCII fallback suffices
    # for the hidden Windows entry and visible failure messages.
    s += static_selection_script(project)
    s += ['set gfxmode=' + gfxmode_value(*project['resolution']), 'set gfxpayload=keep',
          'set color_normal=black/black', 'set color_highlight=black/black',
          'terminal_output gfxterm', 'set theme=$neo_theme',
          'export theme', 'set timeout=%d' % timeout, 'set default=0']
    if handoff == 'external_timer':
        # Set the normal-menu return flag BEFORE it starts. Fedora still draws
        # the theme, then returns -1 before it can select/clear a timed entry.
        # Only the no-widget, static blend branch may take this path.
        s.append('normal_exit')
    s.append("menuentry 'Windows' --id animeboot-default {")
    if anim:
        # Animation always uses the original black-to-Linux path. The saved
        # static preference never participates in this branch.
        s += ['  neo_black', '  set gfxpayload=keep', '  set neo_video_args=', '  set neo_display_ok=1']
        if platform == 'bios':
            s += ['  if neo_vbe_payload; then', '    set neo_display_ok=1',
                  '  else', '    set neo_display_ok=0', '  fi']
        if rotating:
            # Arm and advance in one environment-block write. The reboot path
            # consumes neo_once before it reaches selection, so never advances twice.
            s += ['  set neo_rotation=$neo_next', '  set neo_once=1',
                  '  if save_env -f $neo_env neo_once neo_rotation; then', '    set neo_once=invalid',
                  '    if load_env -f $neo_env neo_once; then']
        else:
            s += ['  if neo_arm; then', '    set neo_once=invalid',
              '    if load_env -f $neo_env neo_once; then',
              ]
        s += [
              '      if [ $neo_once = 1 -a $neo_display_ok = 1 ]; then',
              '        if linux ($neo_root)/AnimeBootNeo/bzImage $neo_video_args ' + args + '; then',
              '          if initrd '+('$neo_initrd' if rotating else '($neo_root)/AnimeBootNeo/anim_initramfs')+'; then',
              '            boot', '          fi', '        fi', '      fi', '    fi', '  fi', '  neo_clear']
        s.append('  neo_windows')
    else:
        # Visible countdowns deliberately retain the normal timed-entry path:
        # Fedora clears before executing this return, then restores the static
        # background on the way to Windows. The UI explains its brief flash.
        s += ['  if [ "$neo_static_transition" = blend ]; then',
              '    normal_exit', '  else', '    neo_black', '    neo_windows', '  fi']
    s.append('}')
    if nested_menu:
        s.append('fi')
    return ('\n'.join(s) + '\n').encode('utf-8')

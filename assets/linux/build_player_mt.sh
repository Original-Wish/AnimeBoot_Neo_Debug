#!/bin/sh
set -eu
cd "$(dirname "$0")"
cc -O3 -static -pthread animeboot_player.c -ljpeg -llzma -lm -o animeboot_player_x86_64
cc -O2 -static animeboot_init.c -o animeboot_init_x86_64

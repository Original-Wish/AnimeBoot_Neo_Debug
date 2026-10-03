/* Original compact ASCII glyphs drawn for AnimeBoot Neo.
 * GPL-3.0-or-later. No console font, vendor resource or disk access is needed.
 * The caller writes pixels into the same framebuffer used for playback.
 */
#ifndef ANIMEBOOT_DEBUG_FONT_H
#define ANIMEBOOT_DEBUG_FONT_H
#include <stdint.h>
#include <stddef.h>

struct neo_debug_glyph { char c; uint8_t rows[7]; };
static const struct neo_debug_glyph neo_debug_glyphs[] = {
 {' ',{0,0,0,0,0,0,0}}, {'A',{14,17,17,31,17,17,17}},
 {'B',{30,17,17,30,17,17,30}}, {'C',{14,17,16,16,16,17,14}},
 {'D',{30,17,17,17,17,17,30}}, {'E',{31,16,16,30,16,16,31}},
 {'F',{31,16,16,30,16,16,16}}, {'G',{14,17,16,23,17,17,15}},
 {'H',{17,17,17,31,17,17,17}}, {'I',{14,4,4,4,4,4,14}},
 {'J',{7,2,2,2,18,18,12}}, {'K',{17,18,20,24,20,18,17}},
 {'L',{16,16,16,16,16,16,31}}, {'M',{17,27,21,21,17,17,17}},
 {'N',{17,25,25,21,19,19,17}}, {'O',{14,17,17,17,17,17,14}},
 {'P',{30,17,17,30,16,16,16}}, {'Q',{14,17,17,17,21,18,13}},
 {'R',{30,17,17,30,20,18,17}}, {'S',{15,16,16,14,1,1,30}},
 {'T',{31,4,4,4,4,4,4}}, {'U',{17,17,17,17,17,17,14}},
 {'V',{17,17,17,17,17,10,4}}, {'W',{17,17,17,21,21,27,17}},
 {'X',{17,17,10,4,10,17,17}}, {'Y',{17,17,10,4,4,4,4}},
 {'Z',{31,1,2,4,8,16,31}}, {'0',{14,17,19,21,25,17,14}},
 {'1',{4,12,4,4,4,4,14}}, {'2',{14,17,1,2,4,8,31}},
 {'3',{30,1,1,14,1,1,30}}, {'4',{2,6,10,18,31,2,2}},
 {'5',{31,16,16,30,1,1,30}}, {'6',{14,16,16,30,17,17,14}},
 {'7',{31,1,2,4,8,8,8}}, {'8',{14,17,17,14,17,17,14}},
 {'9',{14,17,17,15,1,1,14}}, {'.',{0,0,0,0,0,12,12}},
 {',',{0,0,0,0,0,4,8}}, {':',{0,12,12,0,12,12,0}},
 {';',{0,12,12,0,4,4,8}}, {'-',{0,0,0,31,0,0,0}},
 {'_',{0,0,0,0,0,0,31}}, {'=',{0,0,31,0,31,0,0}},
 {'+',{0,4,4,31,4,4,0}}, {'/',{1,1,2,4,8,16,16}},
 {'\\',{16,16,8,4,2,1,1}}, {'>',{16,8,4,2,4,8,16}},
 {'<',{1,2,4,8,4,2,1}}, {'(',{2,4,8,8,8,4,2}},
 {')',{8,4,2,2,2,4,8}}, {'[',{14,8,8,8,8,8,14}},
 {']',{14,2,2,2,2,2,14}}, {'%',{17,2,4,4,8,16,17}},
 {'?',{14,17,1,2,4,0,4}}, {'!',{4,4,4,4,4,0,4}},
 {'*',{0,21,14,31,14,21,0}}, {'#',{10,10,31,10,31,10,10}},
 {'"',{10,10,10,0,0,0,0}}, {'\'',{4,4,8,0,0,0,0}}
};

typedef void (*neo_debug_pixel_fn)(void *, uint32_t, uint32_t, int);

static const uint8_t *neo_debug_bitmap(char c) {
    if (c >= 'a' && c <= 'z') c = (char)(c - 'a' + 'A');
    for (size_t i=0; i<sizeof(neo_debug_glyphs)/sizeof(neo_debug_glyphs[0]); ++i)
        if (neo_debug_glyphs[i].c == c) return neo_debug_glyphs[i].rows;
    static const uint8_t replacement[7]={14,17,1,2,4,0,4};
    return replacement;
}

/* Glyphs use a 6 x 16 cell: tall strokes remain legible at 640 x 480.
 * Larger framebuffers automatically scale the same original glyphs.
 */
static void neo_debug_draw_text(void *opaque, uint32_t width, uint32_t height,
                                const char *text, neo_debug_pixel_fn pixel) {
    uint32_t longest=0, columns=0, lines=1;
    for (const char *p=text; *p; ++p) {
        if (*p=='\n') { if(columns>longest)longest=columns; columns=0; ++lines; }
        else if (*p!='\r') ++columns;
    }
    if(columns>longest)longest=columns;
    uint32_t scale=1, margin=16;
    while(scale<3 && (uint64_t)longest*6*(scale+1)+margin*2<=width &&
          (uint64_t)lines*16*(scale+1)+margin*2<=height) ++scale;
    uint32_t x=margin,y=margin,line=0;
    for (const char *p=text; *p; ++p) {
        if(*p=='\r')continue;
        if(*p=='\n') { x=margin;y+=16*scale;++line;continue; }
        const uint8_t *glyph=neo_debug_bitmap(*p);
        for(uint32_t row=0;row<7;++row) for(uint32_t col=0;col<5;++col) {
            if(!(glyph[row]&(1u<<(4-col))))continue;
            for(uint32_t sy=0;sy<2*scale;++sy)for(uint32_t sx=0;sx<scale;++sx) {
                uint32_t px=x+col*scale+sx,py=y+row*2*scale+sy;
                if(px<width && py<height)pixel(opaque,px,py,line==0);
            }
        }
        x+=6*scale;
    }
}
#endif

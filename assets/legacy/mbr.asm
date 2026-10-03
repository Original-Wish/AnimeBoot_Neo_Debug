; AnimeBoot Neo BIOS bootstrap, independently authored for this project.
; Copies itself below 7C00, then loads the active partition's existing VBR.
; No GRUB core, filesystem resources or state are stored outside the partition.
; The installer replaces bytes 0..439 only; disk identity and partition table stay.
bits 16
org 0x600
 xor ax,ax
 mov ds,ax
 mov es,ax
 cli
 mov ss,ax
 mov sp,0x7c00
 sti
 cld
 mov si,0x7c00
 mov di,0x600
 mov cx,256
 rep movsw
 jmp 0:relocated
relocated:
 mov [drive],dl
 mov si,0x600+446
 mov cx,4
find:
 cmp byte [si],0x80
 je boot
 add si,16
 loop find
 jmp failed
boot:
 mov [partition],si
 mov eax,[si+8]
 mov [dap+8],eax
 mov bx,0x55aa
 mov ah,0x41
 int 0x13
 jc chs
 cmp bx,0xaa55
 jne chs
 test cx,1
 jz chs
 mov si,dap
 mov dl,[drive]
 mov ah,0x42
 int 0x13
 jnc loaded
 jmp failed
chs:
 mov si,[partition]
 mov dh,[si+1]
 mov cx,[si+2]
 mov dl,[drive]
 mov bx,0x7c00
 mov ax,0x0201
 int 0x13
 jc failed
loaded:
 cmp word [0x7c00+510],0xaa55
 jne failed
 mov dl,[drive]
 mov si,[partition]
 jmp 0:0x7c00
failed:
 mov si,message
print:
 lodsb
 test al,al
 jz halt
 mov ah,0x0e
 mov bx,7
 int 0x10
 jmp print
halt:
 cli
 hlt
 jmp halt
message: db 'AnimeBoot Neo: partition boot failed',0
drive: db 0
partition: dw 0
align 4,db 0
dap: db 16,0
 dw 1,0x7c00,0
 dq 0
times 440-($-$$) db 0
times 70 db 0
dw 0xaa55

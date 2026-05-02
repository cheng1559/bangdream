.global _start
.text
_start:
    mov (%rsp), %r12
    cmp $3, %r12
    jl exit_usage
    mov 16(%rsp), %r12
    mov 24(%rsp), %r13
    sub $128, %rsp
    mov %rsp, %r14

    mov $257, %eax
    mov $-100, %edi
    mov %r12, %rsi
    mov $1, %edx
    xor %r10d, %r10d
    syscall
    test %rax, %rax
    js exit_open_device
    mov %rax, %r15

    mov $257, %eax
    mov $-100, %edi
    mov %r13, %rsi
    xor %edx, %edx
    xor %r10d, %r10d
    syscall
    test %rax, %rax
    js exit_open_schedule
    mov %rax, %r13

    mov $228, %eax
    mov $1, %edi
    lea 56(%r14), %rsi
    syscall
    test %rax, %rax
    js exit_clock

record_loop:
    xor %r12d, %r12d

read_loop:
    mov $0, %eax
    mov %r13, %rdi
    lea (%r14,%r12), %rsi
    mov $16, %edx
    sub %r12, %rdx
    syscall
    test %rax, %rax
    js exit_read
    jz eof
    add %rax, %r12
    cmp $16, %r12
    jne read_loop

    mov (%r14), %rax
    xor %edx, %edx
    mov $1000000000, %ecx
    div %rcx
    mov 56(%r14), %r8
    add %rax, %r8
    mov 64(%r14), %r9
    add %rdx, %r9
    cmp $1000000000, %r9
    jl target_ready
    sub $1000000000, %r9
    inc %r8

target_ready:
    mov %r8, 40(%r14)
    mov %r9, 48(%r14)

sleep_loop:
    mov $230, %eax
    mov $1, %edi
    mov $1, %esi
    lea 40(%r14), %rdx
    xor %r10d, %r10d
    syscall
    cmp $-4, %rax
    je sleep_loop
    test %rax, %rax
    js exit_sleep

    movq $0, 16(%r14)
    movq $0, 24(%r14)
    mov 8(%r14), %rax
    mov %rax, 32(%r14)

    xor %r12d, %r12d

write_loop:
    mov $1, %eax
    mov %r15, %rdi
    lea 16(%r14,%r12), %rsi
    mov $24, %edx
    sub %r12, %rdx
    syscall
    test %rax, %rax
    jle exit_write
    add %rax, %r12
    cmp $24, %r12
    jne write_loop
    jmp record_loop

eof:
    test %r12, %r12
    jnz exit_partial
    xor %edi, %edi
    jmp exit

exit_usage:
    mov $2, %edi
    jmp exit
exit_open_device:
    mov $3, %edi
    jmp exit
exit_open_schedule:
    mov $4, %edi
    jmp exit
exit_clock:
    mov $5, %edi
    jmp exit
exit_read:
    mov $6, %edi
    jmp exit
exit_write:
    mov $7, %edi
    jmp exit
exit_partial:
    mov $8, %edi
    jmp exit
exit_sleep:
    mov $9, %edi
exit:
    mov $60, %eax
    syscall

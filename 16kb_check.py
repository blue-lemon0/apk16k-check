# -*- coding: utf-8 -*-
"""apk16k-check: APK 16KB 分页兼容性检查（双层：ZIP 条目对齐 + ELF 段对齐）。

用法:
    python 16kb_check.py <xxx.apk>            完整详情 + 汇总表
    python 16kb_check.py <xxx.apk> --summary  只输出汇总表

判定标准（与 Android 16 官方规则一致）:
  ZIP 层: 未压缩(stored) 的 .so 条目数据必须起始于 16384 字节对齐偏移
          （用 local file header 里的 extra 长度算真实偏移，等价 zipalign -P 16）
  ELF 层: 每个 PT_LOAD 段需 p_align >= 16384 且 p_offset%16K == p_vaddr%16K
"""

import zipfile, struct, sys, io, os

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

PAGE = 16384


def usage():
    print('用法: python 16kb_check.py <xxx.apk> [--summary]')
    sys.exit(1)


def zip_data_offset(fh, zinfo):
    """条目数据偏移 = 本地头偏移 + 30 + 本地头内文件名长 + 本地头内 extra 长。

    中央目录里的 extra 不等于本地 extra，必须读本地头才能对齐 zipalign 的算法。
    返回 (数据偏移, 压缩方法)；解析失败返回 (None, None)。
    """
    fh.seek(zinfo.header_offset)
    lh = fh.read(30)
    if lh[:4] != b'PK\x03\x04':
        return None, None
    fnlen = struct.unpack('<H', lh[26:28])[0]
    exlen = struct.unpack('<H', lh[28:30])[0]
    method = struct.unpack('<H', lh[8:10])[0]
    return zinfo.header_offset + 30 + fnlen + exlen, method


def parse_elf(data):
    """返回 {is_elf, bits, loads:[(offset, vaddr, align), ...]}。"""
    out = {'is_elf': False, 'bits': 0, 'loads': []}
    if len(data) < 4 or data[:4] != b'\x7fELF':
        return out
    out['is_elf'] = True
    ei = data[4]
    if ei == 2:  # ELF64
        if len(data) < 58:
            return out
        out['bits'] = 64
        phoff = struct.unpack('<Q', data[32:40])[0]
        entsz = struct.unpack('<H', data[54:56])[0]
        num = struct.unpack('<H', data[56:58])[0]
        for i in range(num):
            off = phoff + i * entsz
            if off + 56 > len(data):
                break
            if struct.unpack('<I', data[off:off+4])[0] == 1:  # PT_LOAD
                out['loads'].append((
                    struct.unpack('<Q', data[off+8:off+16])[0],   # p_offset
                    struct.unpack('<Q', data[off+16:off+24])[0],  # p_vaddr
                    struct.unpack('<Q', data[off+48:off+56])[0],  # p_align
                ))
    elif ei == 1:  # ELF32
        if len(data) < 46:
            return out
        out['bits'] = 32
        phoff = struct.unpack('<I', data[28:32])[0]
        entsz = struct.unpack('<H', data[42:44])[0]
        num = struct.unpack('<H', data[44:46])[0]
        for i in range(num):
            off = phoff + i * entsz
            if off + 32 > len(data):
                break
            if struct.unpack('<I', data[off:off+4])[0] == 1:  # PT_LOAD
                out['loads'].append((
                    struct.unpack('<I', data[off+4:off+8])[0],    # p_offset
                    struct.unpack('<I', data[off+8:off+12])[0],   # p_vaddr
                    struct.unpack('<I', data[off+28:off+32])[0],  # p_align
                ))
    return out


def elf_16k_ok(loads):
    """模拟 Android linker 检查：每个 PT_LOAD p_align>=16K 且 off%16K==vaddr%16K。"""
    if not loads:
        return True, 'no PT_LOAD'
    for i, (o, v, a) in enumerate(loads):
        if a < PAGE:
            return False, 'LO[%d] align=%d < 16384' % (i, a)
        if (o % PAGE) != (v % PAGE):
            return False, 'LO[%d] off%%16K=%d != vaddr%%16K=%d' % (i, o % PAGE, v % PAGE)
    return True, 'all LOAD pass'


def check_zip(fh, zinfo):
    off, method = zip_data_offset(fh, zinfo)
    if off is None:
        return 'PARSE_FAIL', off, None
    mod = off % PAGE
    if method != 0:                       # deflated：运行时先解压，无 16K 要求
        return 'ok', off, mod
    if mod == 0:
        return 'ok', off, mod
    return 'BAD', off, mod


def analyze(apk_path):
    """返回 {name: {is_so, zip_status, zip_off, elf, elf_ok, elf_reason, data_file}}。"""
    z = zipfile.ZipFile(apk_path)
    fh = open(apk_path, 'rb')
    libs = {}
    for n in sorted(z.namelist()):
        if not n.endswith('.so'):
            continue
        zinfo = z.getinfo(n)
        zs, zoff, zmod = check_zip(fh, zinfo)
        elf = parse_elf(z.read(n))
        data_file = not elf['is_elf']
        if data_file:
            eok, ereason = False, 'NOT-ELF'
        else:
            eok, ereason = elf_16k_ok(elf['loads'])
        libs[n] = {
            'zip_status': zs, 'zip_off': zoff, 'zip_mod': zmod,
            'data_file': data_file, 'elf_ok': eok, 'elf_reason': ereason,
        }
    fh.close()
    return libs


def print_detail(libs):
    print('=' * 100)
    print('  CHECK 1: ZIP entry alignment (16KB rule, matches zipalign -c -P 16)')
    print('=' * 100)
    print()
    print('  Stored (uncompressed) .so entries must start at an offset that is a')
    print('  multiple of 16384. Entries packed with legacy 4KB alignment fail here.')
    print()
    zbad = 0
    for n, r in libs.items():
        if r['zip_status'] == 'BAD':
            status = 'BAD - NOT 16K ALIGNED'
            zbad += 1
        else:
            status = 'OK'
        print('  %-58s off=%-12d mod16384=%-6s %-8s %s'
              % (n, r['zip_off'] if r['zip_off'] is not None else -1,
                 r['zip_mod'] if r['zip_mod'] is not None else -1,
                 'stored', status))
    print()
    print('  ZIP layer: %d .so, %d BAD (needs zipalign -P 16 repack)' % (len(libs), zbad))
    print()

    print('=' * 100)
    print('  CHECK 2: ELF PT_LOAD segment alignment')
    print('=' * 100)
    print()
    ebad = 0
    for n, r in libs.items():
        if r['data_file']:
            print('  %-58s NOT-ELF (data file)' % n)
        elif r['elf_ok']:
            print('  %-58s ELF-OK  (%s)' % (n, r['elf_reason']))
        else:
            ebad += 1
            print('  %-58s ELF-BAD (%s)' % (n, r['elf_reason']))
    print()


def verdict_for(r):
    if r['data_file']:
        return '数据文件(非ELF)'
    if r['zip_status'] == 'BAD' and not r['elf_ok']:
        return '重打包 + 重编译'
    if r['zip_status'] == 'BAD':
        return '重打包 (zipalign -P 16)'
    if not r['elf_ok']:
        return '重编译 (p_align<16K)'
    return '兼容'


def print_consolidated(libs, apk_name):
    print('=' * 100)
    print('  CONSOLIDATED REPORT (%s)' % apk_name)
    print('=' * 100)
    print()
    print('  %-58s %-9s %-9s %s' % ('library', 'ZIP16K', 'ELF16K', '结论'))
    print('  ' + '-' * 92)
    for n, r in libs.items():
        zs = r['zip_status']
        es = '非ELF' if r['data_file'] else ('BAD' if not r['elf_ok'] else 'ok  ')
        print('  %-58s %-9s %-9s %s' % (n, zs, es, verdict_for(r)))

    zbad = [n for n, r in libs.items() if r['zip_status'] == 'BAD']
    ebad = [n for n, r in libs.items() if not r['data_file'] and not r['elf_ok']]
    need = sorted(set(zbad) | set(ebad))
    print()
    print('  需动作的 .so 总数: %d (ZIP 层 %d 个重打包, ELF 层 %d 个重编译, 上表按动作并集)'
          % (len(need), len(zbad), len(ebad)))
    print()
    zok = [n for n, r in libs.items() if r['zip_status'] == 'ok']
    if zok:
        print('  ZIP 层 16K 对齐 OK 的 (无需重打包):')
        for n in zok:
            print('    - %s' % n)
        print()


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    flags = set(a for a in sys.argv[1:] if a.startswith('--'))
    if not args:
        usage()
    apk_path = args[0]
    if not os.path.isfile(apk_path):
        print('文件不存在:', apk_path)
        sys.exit(1)
    apk_name = os.path.basename(apk_path)
    libs = analyze(apk_path)

    if '--summary' not in flags:
        print_detail(libs)
    print_consolidated(libs, apk_name)


if __name__ == '__main__':
    main()
#!/usr/bin/env python3
"""Read-only LAN L2 probe (AF_PACKET). No config changes, no DHCP requests.
Needs root / CAP_NET_RAW -> run on the NAS host via sudo -A.

  l2_probe.py bcast    <iface> <sec>                     broadcast/multicast noise profile
  l2_probe.py ra       <iface> <sec>                     ICMPv6 RAs: sources + decoded fields
  l2_probe.py macprof  <iface> <sec>                     frames per source MAC
  l2_probe.py arp      <iface> <sec>                     ARP watcher, flags IP answered by >1 MAC
  l2_probe.py arpdetail<iface> <sec> <mac>               every ARP frame from a MAC (op + timestamps)
  l2_probe.py dhcp     <iface> <sec>                     DHCP 67/68 watcher (2nd DHCP server check)
  l2_probe.py arpprobe <iface> <src_ip> <src_mac> <ip,ip,...> [rounds=3] [gap=1.5] [dst_mac=broadcast]
                                                          active ARP + replies; dst_mac pins the
                                                          neighbour cache to one MAC (controlled test)
"""
import socket
import struct
import sys
import time
import collections

ETH_P_ALL = 0x0003

def mac(b):
    return ':'.join('%02x' % x for x in b)

def ip(b):
    return '.'.join(str(x) for x in b)

def listen(iface):
    s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.ntohs(ETH_P_ALL))
    s.bind((iface, 0))
    s.settimeout(1.0)
    return s

def etype(data):
    return struct.unpack('!H', data[12:14])[0]

# ---------------------------------------------------------------- bcast

def m_bcast(iface, secs):
    s = listen(iface)
    end = time.time() + secs
    total = bc = mc = uni = 0
    by_src = collections.Counter()
    by_et = collections.Counter()
    arp = mdns = llmnr = nbns = ip6mc = 0
    while time.time() < end:
        try:
            data, _ = s.recvfrom(65535)
        except socket.timeout:
            continue
        total += 1
        if len(data) < 14:
            continue
        dmac, et = data[0:6], etype(data)
        if dmac == b'\xff' * 6:
            bc += 1
        elif dmac[0] & 1:
            mc += 1
        else:
            uni += 1
            continue
        by_src[mac(data[6:12])] += 1
        by_et['0x%04x' % et] += 1
        if et == 0x0806:
            arp += 1
        elif et == 0x86dd:
            ip6mc += 1
        elif et == 0x0800 and len(data) >= 42:
            try:
                ihl = (data[14] & 0x0f) * 4
                dport = struct.unpack('!H', data[14 + ihl + 2:14 + ihl + 4])[0]
                mdns += dport == 5353
                llmnr += dport == 5355
                nbns += dport in (137, 138)
            except Exception:
                pass
    print('=== bcast %s %.0fs: total=%d bcast=%d mcast=%d unicast-to-host=%d (b+m=%.1f%%)'
          % (iface, secs, total, bc, mc, uni, 100.0 * (bc + mc) / total if total else 0))
    print('by source MAC: %s' % dict(by_src.most_common(15)))
    print('by ethertype: %s' % dict(by_et))
    print('ARP=%d IPv6mcast=%d mDNS=%d LLMNR=%d NetBIOS=%d' % (arp, ip6mc, mdns, llmnr, nbns))

# ---------------------------------------------------------------- ra

def m_ra(iface, secs):
    s = listen(iface)
    end = time.time() + secs
    seen = collections.Counter()
    detail = []
    total = 0
    while time.time() < end:
        try:
            data, _ = s.recvfrom(65535)
        except socket.timeout:
            continue
        total += 1
        if len(data) < 54 or etype(data) != 0x86dd:
            continue
        ip6 = data[14:]
        if (ip6[0] >> 4) != 6 or ip6[6] != 58:
            continue
        icmp = ip6[40:]
        if len(icmp) < 16 or icmp[0] != 134:
            continue
        src = socket.inet_ntop(socket.AF_INET6, ip6[8:24])
        curhop, flags = icmp[4], icmp[5]
        life = struct.unpack('!H', icmp[6:8])[0]
        pref = {1: 'HIGH', 3: 'LOW', 0: 'MEDIUM'}.get((curhop & 0x18) >> 3, '?')
        opts = []
        i = 16
        while i + 2 <= len(icmp):
            otype, olen = icmp[i], icmp[i + 1] * 8
            if olen == 0:
                break
            if otype == 3 and i + 32 <= len(icmp):
                pfx = socket.inet_ntop(socket.AF_INET6, icmp[i + 16:i + 32])
                pf = icmp[i + 3]
                opts.append('%s/%d (L=%d A=%d)' % (pfx, icmp[i + 2], (pf >> 7) & 1, (pf >> 6) & 1))
            elif otype == 1:
                opts.append('SRC-LLADDR=' + mac(icmp[i + 2:i + 8]))
            elif otype == 5:
                opts.append('MTU=%d' % struct.unpack('!I', icmp[i + 4:i + 8])[0])
            i += olen
        seen[src] += 1
        detail.append('src=%s mac=%s flags=%02x curhop=%02x pref=%s router_life=%ds | %s'
                      % (src, mac(data[6:12]), flags, curhop, pref, life, ', '.join(opts)))
    print('=== ra %s %.0fs: frames=%d, %d distinct RA sources ===' % (iface, secs, total, len(seen)))
    for k, v in seen.most_common():
        print('  %-45s x%d' % (k, v))
    for d in detail[:12]:
        print('  ' + d)
    print('  NOTE: Router Lifetime 0 => sender does NOT offer a default route.')

# ---------------------------------------------------------------- macprof

def m_macprof(iface, secs):
    s = listen(iface)
    end = time.time() + secs
    by_src, by_et, total = collections.Counter(), collections.defaultdict(collections.Counter), 0
    while time.time() < end:
        try:
            data, _ = s.recvfrom(65535)
        except socket.timeout:
            continue
        total += 1
        if len(data) < 14:
            continue
        src = mac(data[6:12])
        by_src[src] += 1
        by_et[src]['0x%04x' % etype(data)] += 1
    print('=== macprof %s %.0fs total=%d ===' % (iface, secs, total))
    for k, v in by_src.most_common(20):
        print('  %-20s %7d  %s' % (k, v, dict(by_et[k])))

# ---------------------------------------------------------------- arp

def m_arp(iface, secs):
    s = listen(iface)
    end = time.time() + secs
    answers = collections.defaultdict(collections.Counter)
    n = 0
    while time.time() < end:
        try:
            data, _ = s.recvfrom(65535)
        except socket.timeout:
            continue
        if len(data) < 42 or etype(data) != 0x0806:
            continue
        a = data[14:42]
        n += 1
        if struct.unpack('!H', a[6:8])[0] == 2:
            answers[ip(a[14:18])][mac(a[8:14])] += 1
            print('reply: %s is-at %s (target %s)' % (ip(a[14:18]), mac(a[8:14]), ip(a[24:28])))
    print('=== arp %s %.0fs: %d ARP frames ===' % (iface, secs, n))
    for k, c in sorted(answers.items()):
        print('  %-16s -> %s%s' % (k, dict(c), '  <== MULTIPLE MACs' if len(c) > 1 else ''))

# ---------------------------------------------------------------- arpdetail

def m_arpdetail(iface, secs, watch):
    s = listen(iface)
    end = time.time() + secs
    t0, cnt = time.time(), collections.Counter()
    while time.time() < end:
        try:
            data, _ = s.recvfrom(65535)
        except socket.timeout:
            continue
        if len(data) < 42 or etype(data) != 0x0806:
            continue
        a = data[14:42]
        if mac(a[8:14]).lower() != watch.lower():
            continue
        op = struct.unpack('!H', a[6:8])[0]
        cnt['op%d' % op] += 1
        kind = {1: 'REQUEST', 2: 'REPLY'}.get(op, str(op))
        print('t+%6.2fs %-8s dstEth=%s sender=%s/%s target=%s/%s'
              % (time.time() - t0, kind, mac(data[0:6]), mac(a[8:14]), ip(a[14:18]),
                 mac(a[18:24]), ip(a[24:28])))
    print('=== arpdetail %s %.0fs: %s ===' % (watch, secs, dict(cnt)))
    print('  0 frames / only op2 to broadcasts => not a gratuitous-ARP poisoner.')

# ---------------------------------------------------------------- dhcp

def m_dhcp(iface, secs):
    s = listen(iface)
    end = time.time() + secs
    n = 0
    servers, clients = collections.Counter(), collections.Counter()
    while time.time() < end:
        try:
            data, _ = s.recvfrom(65535)
        except socket.timeout:
            continue
        if len(data) < 42 or etype(data) != 0x0800:
            continue
        ihl = (data[14] & 0x0f) * 4
        if data[14 + 9] != 17:
            continue
        udp = data[14 + ihl:]
        if len(udp) < 8:
            continue
        sport, dport = struct.unpack('!HH', udp[0:4])
        if 67 not in (sport, dport) and 68 not in (sport, dport):
            continue
        n += 1
        op = udp[8] if len(udp) > 8 else -1
        print('DHCP: src=%s %d->%d op=%d' % (mac(data[6:12]), sport, dport, op))
        (servers if op == 2 else clients)[mac(data[6:12])] += 1
    print('=== dhcp %s %.0fs: %d frames; servers=%s clients=%s'
          % (iface, secs, n, dict(servers), dict(clients)))

# ---------------------------------------------------------------- arpprobe

def m_arpprobe(iface, src_ip, src_mac, targets, rounds=3, gap=1.5, dst_mac='broadcast'):
    mb = bytes.fromhex(src_mac.replace(':', ''))
    db = b'\xff' * 6 if dst_mac == 'broadcast' else bytes.fromhex(dst_mac.replace(':', ''))
    send = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)
    send.bind((iface, 0))
    recv = listen(iface)
    answers = collections.defaultdict(collections.Counter)
    sent = collections.Counter()
    for _ in range(rounds):
        for t in targets:
            f = db + mb + struct.pack('!H', 0x0806)
            f += struct.pack('!HHBBH', 1, 0x0800, 6, 4, 1)
            f += mb + socket.inet_aton(src_ip) + b'\x00' * 6 + socket.inet_aton(t)
            send.send(f)
            sent[t] += 1
        t_end = time.time() + gap
        while time.time() < t_end:
            try:
                data, _ = recv.recvfrom(65535)
            except socket.timeout:
                continue
            if len(data) < 42 or etype(data) != 0x0806:
                continue
            a = data[14:42]
            if struct.unpack('!H', a[6:8])[0] != 2:
                continue
            if ip(a[24:28]) == src_ip:          # only replies addressed to us
                answers[ip(a[14:18])][mac(a[8:14])] += 1
    print('=== arpprobe: dst_mac=%s rounds=%d asks from %s/%s ===' % (dst_mac, rounds, src_ip, src_mac))
    for t in targets:
        print('  %-16s -> %s%s' % (t, dict(answers[t]) or '{} (no reply)',
                                   '   <== MULTIPLE MACs' if len(answers[t]) > 1 else ''))
    print('  Reminder: verify with `ip neigh show <ip>` that the cache actually points at the MAC you probed.')

# ---------------------------------------------------------------- main

if __name__ == '__main__':
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(1)
    mode, iface, secs = sys.argv[1], sys.argv[2], float(sys.argv[3])
    if mode == 'bcast':
        m_bcast(iface, secs)
    elif mode == 'ra':
        m_ra(iface, secs)
    elif mode == 'macprof':
        m_macprof(iface, secs)
    elif mode == 'arp':
        m_arp(iface, secs)
    elif mode == 'arpdetail':
        m_arpdetail(iface, secs, sys.argv[4])
    elif mode == 'dhcp':
        m_dhcp(iface, secs)
    elif mode == 'arpprobe':
        m_arpprobe(iface, sys.argv[4], sys.argv[5], sys.argv[6].split(','),
                   int(sys.argv[7]) if len(sys.argv) > 7 else 3,
                   float(sys.argv[8]) if len(sys.argv) > 8 else 1.5,
                   sys.argv[9] if len(sys.argv) > 9 else 'broadcast')
    else:
        print(__doc__)
        sys.exit(1)

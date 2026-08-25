#! /usr/bin/env python2
## vim:set ts=4 sw=4 et: -*- coding: utf-8 -*-
#
#  mkdata.py -- create data *.h files
#
#  This file is part of the UPX executable compressor.
#
#  Copyright (C) 1996-2017 Markus Franz Xaver Johannes Oberhumer
#  All Rights Reserved.
#
#  UPX and the UCL library are free software; you can redistribute them
#  and/or modify them under the terms of the GNU General Public License as
#  published by the Free Software Foundation; either version 2 of
#  the License, or (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program; see the file COPYING.
#  If not, write to the Free Software Foundation, Inc.,
#  59 Temple Place - Suite 330, Boston, MA 02111-1307, USA.
#
#  Markus F.X.J. Oberhumer              Laszlo Molnar
#  <markus@oberhumer.com>               <ezerotven+github@gmail.com>
#


import getopt, os, re, sys


class opts:
    verbose = 0


class DataWriter:
    def __init__(self, w):
        self.w = w
        self.pos = None

    def w_bol(self, pos):
        self.w("/* 0x%04x */ " % (pos))
        self.pos = pos
    def w_eol(self, fill=""):
        if self.pos is not None:
            self.w(fill.rstrip() + "\n")


class DataWriter_c(DataWriter):
    def w_data(self, data):
        w, n = self.w, len(data)
        for i in range(n):
            if i & 15 == 0:
                self.w_eol()
                self.w_bol(i)
            w("%3d" % ord(data[i]))
            if i != n - 1: w(",")
        self.w_eol()

    def w_data32(self, data):
        w, n = self.w, len(data)
        for i in range(n):
            if i & 3 == 0:
                self.w_eol()
                self.w_bol(i)
            w("0x%08x" % int(data[i]))
            if i != n - 1: w(",")
        self.w_eol()


def write_uint32(fn, data):
    fp = open(fn, "wb")
    #w = fp.write
    def w(x): fp.write(x.encode("utf-8")) # for Python3
    w("/* %d entries */\n" % len(data))
    DataWriter_c(w).w_data32(data)
    fp.close()


def main(argv):
    try: assert 0
    except AssertionError: pass
    else: raise Exception("fatal error - assertions not enabled")
    shortopts, longopts = "qv:", ["quiet", "verbose"]
    xopts, args = getopt.gnu_getopt(argv[1:], shortopts, longopts)
    for opt, optarg in xopts:
        if 0: pass
        elif opt in ["-q", "--quiet"]: opts.verbose = opts.verbose - 1
        elif opt in ["-v", "--verbose"]: opts.verbose = opts.verbose + 1
        else: assert 0, ("getopt problem:", opt, optarg, xopts, args)

    assert len(args) == 0

    N = 16384
    N = 256

    data = [0] * N
    write_uint32("data01.h", data)

    data = [0] * N
    for i in range(len(data)):
        data[i] = i & 255
    write_uint32("data02.h", data)

    data = [0] * N
    a, b = 1, 2
    for i in range(len(data)):
        b = (b ^ (b << 17)) & 0xffffffff
        b = (b ^ a ^ (b >> 7) ^ (a >> 16)) & 0xffffffff
        data[i] = (a + b) & 0xffffffff
        a, b = b, a
    write_uint32("data03.h", data)


if __name__ == "__main__":
    sys.exit(main(sys.argv))

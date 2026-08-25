#! /bin/bash
## vim:set ts=4 sw=4 et:
set -e; set -o pipefail

# Create test files for the UPX test suite.
# Copyright (C) Markus Franz Xaver Johannes Oberhumer

#set -x # debug
umask 022
argv0=$0; argv0abs=$(readlink -en -- "$0"); argv0dir=$(dirname "$argv0abs")

#
# WARNING: these are rather HUGE archives and require a total of > 10 GiB of disk space when unpacked
#
# Required unpacked files:
#
#   https://github.com/upx/upx-stubtools/releases/download/v20210104/bin-upx-20210104.tar.xz
#   https://github.com/upx/upx-stubtools/releases/download/v20160918/upx-linux-musl-gcc-9.2.0-toolchains-20200226.tar.xz
#
# It is preferred to use the /usr/local/bin/upx-linux-musl-gcc-9.2.0-toolchains-20200226
# directory or symlink for installation so that dynamic loading and execution of the
# created binaries on your local machine works (also see "fixed_prefix") in set_gcc() below.
#


check_sha256sums() {
    (cd ../../files/all && sha256sum -b */*upx_test* | LC_ALL=C sort -k2) > .sha256sums.current
    if ! cmp -s .sha256sums.expected .sha256sums.current; then
        echo "UPX-ERROR: $1 FAILED: checksum mismatch"
        diff -u .sha256sums.expected .sha256sums.current || true
        exit 1
    fi
}


search_dir() {
    local subdir="$1"
    local d
    dir=
    for d in "$HOME/local/bin" "$HOME/.local/bin" "$HOME/bin" /usr/local/bin /usr/local/packages; do
        if [[ -d "$d/$subdir" ]]; then
            dir=$d
            break
        fi
    done
}


# sets: gcc, gxx, dynlink_flags, rpath_flags
# uses: d_toolchains, tc, dynlink_name
set_gcc() {
    local mode=$1
    local x fixed_prefix
    x=linux-musl-gcc-7.3.0-20180905/$tc-gcc-7.3.0
    x=linux-musl-gcc-9.2.0-20200226/$tc-gcc-9.2.0
    if [[ $mode == pie ]]; then
        x=linux-musl-gcc-7.3.0-20180905-default-pie/$tc-gcc-7.3.0
        x=linux-musl-gcc-9.2.0-20200226-default-pie/$tc-gcc-9.2.0
    fi
    gcc=$d_toolchains/$x/bin/$tc-gcc
    gxx=$d_toolchains/$x/bin/$tc-g++
    if [[ ! -f $d_toolchains/$x/$tc/lib/$dynlink_name ]]; then
        echo "BAD dynlink $x/$tc/lib/$dynlink_name"
        exit 1
    fi
    # NOTE: to ensure reproducibility we set a fixed dynamic linker and rpath in /usr/local/bin
    fixed_prefix=/usr/local/bin/upx-linux-musl-gcc-7.3.0-toolchains-20180905
    fixed_prefix=/usr/local/bin/upx-linux-musl-gcc-9.2.0-toolchains-20200226
    dynlink_flags="-Wl,--dynamic-linker=$fixed_prefix/$x/$tc/lib/$dynlink_name"
    rpath_flags="-Wl,-rpath=$fixed_prefix/$x/$tc/lib"
    rpath_flags="$rpath_flags -Wl,-rpath=\$ORIGIN"
}


run_gcc() {
    #echo "$@"
    "$@"
}


cmd_build_arch() {
    local src_c src_x odir oprefix tc dynlink_name
    local gcc gxx cppflags cflags cxxflags ldflags libs libs_c libs_x
    local o x

    printf "===== %-10s  %-22s  %s\n" $1 $2 $3

    oprefix=$1
    src_c=src/$1_c*.c
    src_x=src/$1_x*.cpp
    odir=../../files/all/$2
    tc=$3
    dynlink_name=$4
    shift 4
    if [[ $# -gt 0 ]]; then
        oprefix="${oprefix}$1"
        shift
    fi

    mkdir -p $odir

    cppflags="-DMUSL -DDLL_PREFIX=\"$oprefix\""
    cflags="-pthread -O2 -Wall -W -Wcast-align -Wcast-qual -Wshadow -pedantic"
    cxxflags="$cflags"
    ldflags="-s -Wl,--build-id=none"

    #libs_c="-L$odir -l${oprefix}_dll_c"
    libs_c="-DUSE_DLOPEN"
    libs_x="-L$odir -l${oprefix}_dll_x"

    o=$odir/$oprefix

    set_gcc default

    # dll (-fPIC)
    x="$1 -DDLL -shared -fPIC $dynlink_flags $rpath_flags"
    run_gcc $gcc $x $cppflags $cflags   $ldflags -o ${odir}/lib${oprefix}_dll_c.so $src_c $libs
    run_gcc $gxx $x $cppflags $cxxflags $ldflags -o ${odir}/lib${oprefix}_dll_x.so $src_x $libs

    # exe (-fPIC)
    x="$1 -fPIC $dynlink_flags $rpath_flags"
    run_gcc $gcc $x $cppflags $cflags   $ldflags -o ${o}_exe_dynamic_pic_c.out $src_c $libs_c $libs
    run_gcc $gxx $x $cppflags $cxxflags $ldflags -o ${o}_exe_dynamic_pic_x.out $src_x $libs_x $libs

    # exe
    x="$1 $dynlink_flags $rpath_flags"
    run_gcc $gcc $x $cppflags $cflags   $ldflags -o ${o}_exe_dynamic_nopie_c.out $src_c $libs_c $libs
    run_gcc $gxx $x $cppflags $cxxflags $ldflags -o ${o}_exe_dynamic_nopie_x.out $src_x $libs_x $libs

    # static exe
    x="$1 -DSTATIC -static"
    run_gcc $gcc $x $cppflags $cflags   $ldflags -o ${o}_exe_static_nopie_c.out $src_c $libs
    run_gcc $gxx $x $cppflags $cxxflags $ldflags -o ${o}_exe_static_nopie_x.out $src_x $libs

    set_gcc pie

    # exe (-fPIE via --enable-default-pie toolchain)
    x="$1 $dynlink_flags $rpath_flags"
    run_gcc $gcc $x $cppflags $cflags   $ldflags -o ${o}_exe_dynamic_pie_c.out $src_c $libs_c $libs
    run_gcc $gxx $x $cppflags $cxxflags $ldflags -o ${o}_exe_dynamic_pie_x.out $src_x $libs_x $libs

    # static exe (-fPIE via --enable-default-pie toolchain)
    x="$1 -DSTATIC -static"
    run_gcc $gcc $x $cppflags $cflags   $ldflags -o ${o}_exe_static_pie_c.out $src_c $libs
    run_gcc $gxx $x $cppflags $cxxflags $ldflags -o ${o}_exe_static_pie_x.out $src_x $libs
}


__cmd_scanelf_check() {
    local x
    x=$(echo "$1" | egrep '_'$2'_[cx]\.')
    if [[ $3 == ET_EXEC ]]; then
        if echo "$x" | egrep -q -v 'type=ET_EXEC '; then
           echo "$x" | egrep    -v 'type=ET_EXEC '
           echo "ERROR: $2: expected type=ET_EXEC"
           exit 1
        fi
        if echo "$x" | egrep -q    'type=ET_DYN '; then
           echo "$x" | egrep       'type=ET_DYN '
           echo "ERROR: $2: unexpected type=ET_DYN"
           exit 1
        fi
    else
        if echo "$x" | egrep -q -v 'type=ET_DYN '; then
           echo "$x" | egrep    -v 'type=ET_DYN '
           echo "ERROR: $2: expected type=ET_DYN"
           exit 1
        fi
        if echo "$x" | egrep -q    'type=ET_EXEC '; then
           echo "$x" | egrep       'type=ET_EXEC '
           echo "ERROR: $2: unexpected type=ET_EXEC"
           exit 1
        fi
    fi
    if [[ $4 == STATIC ]]; then
        if echo "$x" | egrep -q -v 'bind=STATIC '; then
           echo "$x" | egrep    -v 'bind=STATIC '
           echo "ERROR: $2: expected bind=STATIC"
           exit 1
        fi
        if echo "$x" | egrep -q    'bind=LAZY '; then
           echo "$x" | egrep       'bind=LAZY '
           echo "ERROR: $2: unexpected bind=LAZY"
           exit 1
        fi
    else
        if echo "$x" | egrep -q -v 'bind=LAZY '; then
           echo "$x" | egrep    -v 'bind=LAZY '
           echo "ERROR: $2: expected bind=LAZY"
           exit 1
        fi
        if echo "$x" | egrep -q    'bind=STATIC '; then
           echo "$x" | egrep       'bind=STATIC '
           echo "ERROR: $2: unexpected bind=STATIC"
           exit 1
        fi
    fi
    if [[ $5 == maybe_textrel ]]; then
        true
    elif [[ $5 == no_textrel ]]; then
        if echo "$x" | egrep -q  'textrel=TEXTREL '; then
           echo "$x" | egrep     'textrel=TEXTREL '
           echo "ERROR: $2: unexpected textrel=TEXTREL"
           #exit 1
        fi
    elif [[ $5 == yes_textrel ]]; then
        if echo "$x" | egrep -q -v 'textrel=TEXTREL '; then
           echo "$x" | egrep    -v 'textrel=TEXTREL '
           echo "ERROR: $2: expected textrel=TEXTREL"
           #exit 1
        fi
    else
        echo "ERROR: '$4'"
        exit 1
    fi
}

cmd_scanelf() {
    local scanelf scanelf_format odir x
    scanelf=/opt/cc-i386-linux/packages/pax-utils-0.8.1/bin/scanelf
    scanelf_format="type=%o machine=%a bind=%b textrel=%t file=%f"
    scanelf_format="type=%o bind=%b textrel=%t file=%f"

    printf "===== %-10s  %-22s  %s\n" $1 $2 $3

    odir=../../files/all/$2
    x=$($scanelf --nobanner -F "$scanelf_format" $odir/*$1*.* | sed 's/ *$//')

    # print all TEXTREL
    if echo "$x" | egrep -q 'textrel=TEXTREL '; then
       #echo "$x" | egrep    'textrel=TEXTREL '
       true
    fi

    __cmd_scanelf_check "$x" dll               ET_DYN  LAZY   no_textrel
    __cmd_scanelf_check "$x" exe_dynamic_nopie ET_EXEC LAZY   no_textrel
    __cmd_scanelf_check "$x" exe_dynamic_pic   ET_EXEC LAZY   no_textrel
    __cmd_scanelf_check "$x" exe_dynamic_pie   ET_DYN  LAZY   no_textrel
    __cmd_scanelf_check "$x" exe_static_nopie  ET_EXEC STATIC no_textrel
    __cmd_scanelf_check "$x" exe_static_pie    ET_DYN  LAZY   no_textrel
}


handle_cmd() {
    local cmd="$1"; shift

    #$cmd "$1" i386-linux.elf         i586-linux-musl         ld-musl-i386.so.1          "_i586"
    #return

    $cmd "$1" amd64-linux.elf        x86_64-linux-musl       ld-musl-x86_64.so.1
    $cmd "$1" arm-linux.elf          arm-linux-musleabi      ld-musl-arm.so.1           "_sf_a" "-marm"
    $cmd "$1" arm-linux.elf          arm-linux-musleabi      ld-musl-arm.so.1           "_sf_t" "-mthumb"
    $cmd "$1" arm-linux.elf          arm-linux-musleabihf    ld-musl-armhf.so.1         "_hf_a" "-march=armv6+fp -marm"
    $cmd "$1" arm-linux.elf          arm-linux-musleabihf    ld-musl-armhf.so.1         "_hf_t" "-march=armv6t2+fp -mthumb"
    $cmd "$1" arm64-linux.elf        aarch64-linux-musl      ld-musl-aarch64.so.1
    $cmd "$1" arm64eb-linux.elf      aarch64_be-linux-musl   ld-musl-aarch64_be.so.1
    $cmd "$1" armeb-linux.elf        armeb-linux-musleabi    ld-musl-armeb.so.1         "_sf_a" "-marm"
    $cmd "$1" armeb-linux.elf        armeb-linux-musleabi    ld-musl-armeb.so.1         "_sf_t" "-mthumb"
#   $cmd "$1" armeb-linux.elf        armeb-linux-musleabihf  ld-musl-armebhf.so.1       "_hf_a" "-march=armv6+fp -marm"
#   $cmd "$1" armeb-linux.elf        armeb-linux-musleabihf  ld-musl-armebhf.so.1       "_hf_t" "-march=armv6t2+fp -mthumb"
    $cmd "$1" i386-linux.elf         i586-linux-musl         ld-musl-i386.so.1          "_i586"
    $cmd "$1" i386-linux.elf         i686-linux-musl         ld-musl-i386.so.1          "_i686" "-msse2"
    $cmd "$1" m68k-linux.elf         m68k-linux-musl         ld-musl-m68k.so.1
####$cmd "$1" microblaze-linux.elf   microblaze-linux-musl   ld-musl-microblaze.so.1
####$cmd "$1" microblazeel-linux.elf microblazeel-linux-musl ld-musl-microblazeel.so.1
    $cmd "$1" mips-linux.elf         mips-linux-musl         ld-musl-mips.so.1          "_hf"
    $cmd "$1" mips64-linux.elf       mips64-linux-musl       ld-musl-mips64.so.1        "_hf"
    $cmd "$1" mips64el-linux.elf     mips64el-linux-musl     ld-musl-mips64el.so.1      "_hf"
    $cmd "$1" mipsel-linux.elf       mipsel-linux-musl       ld-musl-mipsel.so.1        "_hf"
    $cmd "$1" powerpc-linux.elf      powerpc-linux-musl      ld-musl-powerpc.so.1       "_hf"
    $cmd "$1" powerpc-linux.elf      powerpc-linux-muslsf    ld-musl-powerpc-sf.so.1    "_sf"
    $cmd "$1" powerpc64-linux.elf    powerpc64-linux-musl    ld-musl-powerpc64.so.1
    $cmd "$1" powerpc64le-linux.elf  powerpc64le-linux-musl  ld-musl-powerpc64le.so.1
    $cmd "$1" s390x-linux.elf        s390x-linux-musl        ld-musl-s390x.so.1
####$cmd "$1" sh-linux.elf           sh-linux-musl           ld-musl-sh-nofpu.so.1      "_sf"
####$cmd "$1" sheb-linux.elf         sheb-linux-musl         ld-musl-sheb-nofpu.so.1    "_sf"
    # ILP32 toolchains on 64-bit machines
    $cmd "$1" amd64-linux-x32.elf    x86_64-linux-muslx32    ld-musl-x32.so.1
    $cmd "$1" mips64-linux-n32.elf   mips64-linux-musln32    ld-musl-mipsn32.so.1       "_hf"
    $cmd "$1" mips64el-linux-n32.elf mips64el-linux-musln32  ld-musl-mipsn32el.so.1     "_hf"
}


main() {
    search_dir bin-upx/packages/clang-3.9.0-20160902
    if [[ -z $dir ]]; then
        echo "$argv0: ERROR: 'bin-upx' not found"
        exit 1
    fi
    d_bin_upx=$dir/bin-upx
    echo "info: found bin_upx=$d_bin_upx"

    $d_bin_upx/upx-stubtools-check-version 20210104

    search_dir upx-linux-musl-gcc-7.3.0-toolchains-20180905/linux-musl-gcc-7.3.0-20180905/x86_64-linux-musl-gcc-7.3.0
    search_dir upx-linux-musl-gcc-9.2.0-toolchains-20200226/linux-musl-gcc-9.2.0-20200226/x86_64-linux-musl-gcc-9.2.0
    if [[ -z $dir ]]; then
        echo "$argv0: ERROR: 'upx-linux-musl-gcc-7.3.0-toolchains-20180905' not found"
        echo "$argv0: ERROR: 'upx-linux-musl-gcc-9.2.0-toolchains-20200226' not found"
        exit 1
    fi
    d_toolchains=$dir/upx-linux-musl-gcc-7.3.0-toolchains-20180905
    d_toolchains=$dir/upx-linux-musl-gcc-9.2.0-toolchains-20200226
    echo "info: found toolchains=$d_toolchains"

    case $1 in
    "" | build | --build)
        handle_cmd cmd_build_arch upx_test01
        check_sha256sums
        cp -p .sha256sums.expected ../../files/all/
        echo "UPX test files built. All done."
        ;;
    scanelf | --scanelf)
        echo "checking sha256sums..."
        check_sha256sums
        echo "checking sha256sums DONE"
        handle_cmd cmd_scanelf upx_test01
        ;;
    *)
        echo "$argv0: USAGE ERROR: '$1'"
        exit 1
        ;;
    esac
}

main "$@"

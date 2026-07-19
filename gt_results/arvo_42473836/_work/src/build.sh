for branch in master next
do
    cd capstone$branch
    mkdir build
    cd build
    cmake -DCAPSTONE_BUILD_SHARED=0 ..
    make
    cd $SRC/capstone$branch/bindings/python
    sed -i -e 's/#print/print/' capstone/__init__.py
    (
    export CFLAGS=""
    python setup.py install
    )
    cd $SRC/capstone$branch/suite
    mkdir fuzz/corpus
    find MC/ -name *.cs | ./test_corpus.py
    cd fuzz
    zip -r fuzz_disasm"$branch"_seed_corpus.zip corpus/
    cp fuzz_disasm"$branch"_seed_corpus.zip $OUT/
    cp fuzz_disasm.options $OUT/fuzz_disasm$branch.options
    $CC $CFLAGS -I../../include/ -c fuzz_disasm.c -o fuzz_disasm.o
    $CXX $CXXFLAGS fuzz_disasm.o -o $OUT/fuzz_disasm$branch ../../build/libcapstone.a -lFuzzingEngine
    cd ../../../
done

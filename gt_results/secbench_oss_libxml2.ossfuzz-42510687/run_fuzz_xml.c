#include <stdio.h>
#include <stdlib.h>
int LLVMFuzzerInitialize(int *argc, char ***argv);
int LLVMFuzzerTestOneInput(const char *data, size_t size);
int main(int argc, char **argv) {
    FILE *f;
    long sz;
    char *buf;
    if (argc != 2) {
        fprintf(stderr, "usage: %s <input>\n", argv[0]);
        return 2;
    }
    f = fopen(argv[1], "rb");
    if (f == NULL) {
        perror("fopen");
        return 2;
    }
    if (fseek(f, 0, SEEK_END) != 0) {
        perror("fseek");
        fclose(f);
        return 2;
    }
    sz = ftell(f);
    if (sz < 0) {
        perror("ftell");
        fclose(f);
        return 2;
    }
    if (fseek(f, 0, SEEK_SET) != 0) {
        perror("fseek");
        fclose(f);
        return 2;
    }
    buf = malloc((size_t)sz);
    if (buf == NULL) {
        fprintf(stderr, "malloc failed\n");
        fclose(f);
        return 2;
    }
    if (fread(buf, 1, (size_t)sz, f) != (size_t)sz) {
        fprintf(stderr, "fread failed\n");
        free(buf);
        fclose(f);
        return 2;
    }
    fclose(f);
    LLVMFuzzerInitialize(NULL, NULL);
    LLVMFuzzerTestOneInput(buf, (size_t)sz);
    free(buf);
    return 0;
}

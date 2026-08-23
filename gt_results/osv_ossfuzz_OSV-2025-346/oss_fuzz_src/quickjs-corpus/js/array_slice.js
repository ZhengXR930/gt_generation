var ref, a, i, len;
len = 1000;
ref = [];
for (i = 0; i < len; i++) ref[i] = i;
ref[0] = 42;
a = ref.slice();
a[0] = 0;

var tab, ref, i, j, len;
len = 1000;
ref = [];
for (i = 0; i < len; i++) ref[i] = i;
tab = ref.slice();
for (i = len; i-- > 0;) tab.length = i;

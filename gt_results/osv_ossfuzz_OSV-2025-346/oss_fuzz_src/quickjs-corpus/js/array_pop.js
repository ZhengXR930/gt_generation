var tab, ref, i, j, len, sum;
len = 500;
ref = [];
for (i = 0; i < len; i++) ref[i] = i;
tab = ref.slice();
sum = 0;
for (i = 0; i < len; i++) sum += tab.pop();

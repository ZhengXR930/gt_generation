var r, i, sum, len = 100;
r = [];
for (i = 0; i < len; i++) r[i] = i;
sum = 0;
for (i of r) {
    sum += i;
}

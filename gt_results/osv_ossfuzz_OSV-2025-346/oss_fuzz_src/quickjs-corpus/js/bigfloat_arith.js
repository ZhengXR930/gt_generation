var i, sum, a, incr, a0;
a0 = BigFloat("0.1");
incr = BigFloat("1.1");
sum = 0;
a = a0;
for (i = 0; i < 1000; i++) {
    sum += a * a;
    a += incr;
}

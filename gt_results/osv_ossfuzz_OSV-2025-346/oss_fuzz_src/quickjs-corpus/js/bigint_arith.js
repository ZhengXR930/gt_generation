function bigint_arith(bits) {
    var i, j, sum, a, incr, a0, sum0;
    sum0 = BigInt(0);
    a0 = BigInt(1) << BigInt(Math.floor((bits - 10) * 0.5));
    incr = BigInt(1);
    sum = sum0;
    a = a0;
    for (i = 0; i < 1000; i++) {
        sum += a * a;
        a += incr;
    }
}

bigint_arith(64);
bigint_arith(256);
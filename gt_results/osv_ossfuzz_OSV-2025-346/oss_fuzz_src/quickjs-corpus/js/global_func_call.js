function g(a) {
    return 1;
}

function global_func_call(n) {
    var sum = 0;
    sum += g(n);
    return sum;
}

global_func_call();

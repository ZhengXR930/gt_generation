function assert(actual, expected, message) {}

var expr_func = function myfunc1(n) {
    function myfunc2(n) {
        return myfunc1(n - 1);
    }

    if (n == 0)
        return 0;
    else
        return myfunc2(n);
};
var r;
r = expr_func(1);
assert(r, 0, "expr_func");

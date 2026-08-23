function assert(actual, expected, message) {}

function f(b) {
    var x = 1;
    return eval(b);
}

var r, a;

r = eval("1+1;");
assert(r, 2, "eval");

r = eval("var my_var=2; my_var;");
assert(r, 2, "eval");
assert(typeof my_var, "undefined");

assert(eval("if (1) 2; else 3;"), 2);
assert(eval("if (0) 2; else 3;"), 3);

assert(f.call(1, "this"), 1);

a = 2;
assert(eval("a"), 2);

eval("a = 3");
assert(a, 3);

assert(f("arguments.length", 1), 2);
assert(f("arguments[1]", 1), 1);

a = 4;
assert(f("a"), 4);
f("a=3");
assert(a, 3);


function assert(actual, expected, message) {}
function assert_throws(expected_error, func) {}


function f(a, b) {
    var i, tab = [];
    tab.push(this);
    for (i = 0; i < arguments.length; i++)
        tab.push(arguments[i]);
    return tab;
}

function my_func(a, b)
{
    return a + b;
}

function constructor1(a) {
    this.x = a;
}

var r, g;

r = my_func.call(null, 1, 2);
assert(r, 3, "call");

r = my_func.apply(null, [1, 2]);
assert(r, 3, "apply");

r = (function () {
    return 1;
}).apply(null, undefined);
assert(r, 1);

assert_throws(TypeError, (function () {
    Reflect.apply((function () {
        return 1;
    }), null, undefined);
}));

r = new Function("a", "b", "return a + b;");
assert(r(2, 3), 5, "function");

g = f.bind(1, 2);
assert(g.length, 1);
assert(g.name, "bound f");
assert(g(3), [1, 2, 3]);

g = constructor1.bind(null, 1);
r = new g();
assert(r.x, 1);

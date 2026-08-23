function assert(actual, expected, message) {}
function assert_throws(expected_error, func) {}

var f;
var c = "global";

(function () {
    "use strict";
    // XXX: node only throws in strict mode
    f = function (a = eval("var arguments")) {
    };
    assert_throws(SyntaxError, f);
})();

f = function (a = eval("1"), b = arguments[0]) {
    return b;
};
assert(f(12), 12);

f = function (a, b = arguments[0]) {
    return b;
};
assert(f(12), 12);

f = function (a, b = () => arguments) {
    return b;
};
assert(f(12)()[0], 12);

f = function (a = eval("1"), b = () => arguments) {
    return b;
};
assert(f(12)()[0], 12);

(function () {
    "use strict";
    f = function (a = this) {
        return a;
    };
    assert(f.call(123), 123);

    f = function f(a = f) {
        return a;
    };
    assert(f(), f);

    f = function f(a = eval("f")) {
        return a;
    };
    assert(f(), f);
})();

f = (a = eval("var c = 1"), probe = () => c) => {
    var c = 2;
    assert(c, 2);
    assert(probe(), 1);
}
f();

f = (a = eval("var arguments = 1"), probe = () => arguments) => {
    var arguments = 2;
    assert(arguments, 2);
    assert(probe(), 1);
}
f();

f = function f(a = eval("var c = 1"), b = c, probe = () => c) {
    assert(b, 1);
    assert(c, 1);
    assert(probe(), 1)
}
f();

assert(c, "global");
f = function f(a, b = c, probe = () => c) {
    eval("var c = 1");
    assert(c, 1);
    assert(b, "global");
    assert(probe(), "global")
}
f();
assert(c, "global");

f = function f(a = eval("var c = 1"), probe = (d = eval("c")) => d) {
    assert(probe(), 1)
}
f();

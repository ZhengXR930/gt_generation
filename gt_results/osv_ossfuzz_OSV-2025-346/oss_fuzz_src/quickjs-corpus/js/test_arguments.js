function assert(actual, expected, message) {}

function f2() {
    assert(arguments.length, 2, "arguments");
    assert(arguments[0], 1, "arguments");
    assert(arguments[1], 3, "arguments");
}

f2(1, 3);

function assert(actual, expected, message) {}

var o1 = {x: "o1", y: "o1"};
var x = "local";
eval('var z="var_obj";');
assert(z === "var_obj");
with (o1) {
    assert(x === "o1");
    assert(eval("x") === "o1");
    var f = function () {
        o2 = {x: "o2"};
        with (o2) {
            assert(x === "o2");
            assert(y === "o1");
            assert(z === "var_obj");
            assert(eval("x") === "o2");
            assert(eval("y") === "o1");
            assert(eval("z") === "var_obj");
            assert(eval('eval("x")') === "o2");
        }
    };
    f();
}

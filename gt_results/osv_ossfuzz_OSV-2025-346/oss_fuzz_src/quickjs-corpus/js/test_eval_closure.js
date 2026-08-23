function assert(actual, expected, message) {}

var tab;

tab = [];
for (let i = 0; i < 3; i++) {
    eval("tab.push(function g1() { return i; })");
}
for (let i = 0; i < 3; i++) {
    assert(tab[i]() === i);
}

tab = [];
for (let i = 0; i < 3; i++) {
    let f = function f() {
        eval("tab.push(function g2() { return i; })");
    };
    f();
}
for (let i = 0; i < 3; i++) {
    assert(tab[i]() === i);
}

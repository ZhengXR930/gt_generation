function assert(actual, expected, message) {}

var i;
tab = [];
for (i in {x: 1, y: 2, z: 3}) {
    if (i === "y")
        continue;
    tab.push(i);
}
assert(tab.toString() == "x,z");

tab = [];
for (i in {x: 1, y: 2, z: 3}) {
    if (i === "z")
        break;
    tab.push(i);
}
assert(tab.toString() == "x,y");

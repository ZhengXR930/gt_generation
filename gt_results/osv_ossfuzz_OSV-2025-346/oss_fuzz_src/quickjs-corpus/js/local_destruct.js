var j, v1, v2, v3, v4;
var array = [1, 2, 3, 4, 5];
var o = {a: 1, b: 2, c: 3, d: 4};
var a, b, c, d;
[v1, v2, , v3, ...v4] = array;
({a, b, c, d} = o);
({a: a, b: b, c: c, d: d} = o);

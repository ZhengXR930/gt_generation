function assert(actual, expected, message) {}

var a, s;
s = '{"x":1,"y":true,"z":null,"a":[1,2,3],"s":"str"}';
a = JSON.parse(s);
assert(a.x, 1);
assert(a.y, true);
assert(a.z, null);
assert(JSON.stringify(a), s);

/* indentation test */
assert(JSON.stringify([[{x: 1, y: {}, z: []}, 2, 3]], undefined, 1),
    `[
 [
  {
   "x": 1,
   "y": {},
   "z": []
  },
  2,
  3
 ]
]`);

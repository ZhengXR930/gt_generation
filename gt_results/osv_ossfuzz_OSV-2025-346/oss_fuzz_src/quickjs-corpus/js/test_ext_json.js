function assert(actual, expected, message) {}


var expected, input, obj;
expected = '{"x":false,"y":true,"z2":null,"a":[1,8,160],"s":"str"}';
input = `{ "x":false, /*comments are allowed */
           "y":true,  // also a comment
           z2:null, // unquoted property names
           "a":[+1,0o10,0xa0,], // plus prefix, octal, hexadecimal
           "s":"str",} // trailing comma in objects and arrays
        `;
obj = std.parseExtJSON(input);
assert(JSON.stringify(obj), expected);

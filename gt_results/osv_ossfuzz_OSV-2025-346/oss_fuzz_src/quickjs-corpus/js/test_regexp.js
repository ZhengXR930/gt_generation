function assert(actual, expected, message) {}

var a, str;
str = "abbbbbc";
a = /(b+)c/.exec(str);
assert(a[0], "bbbbbc");
assert(a[1], "bbbbb");
assert(a.index, 1);
assert(a.input, str);
a = /(b+)c/.test(str);
assert(a, true);
assert(/\x61/.exec("a")[0], "a");
assert(/\u0061/.exec("a")[0], "a");
assert(/\ca/.exec("\x01")[0], "\x01");
assert(/\\a/.exec("\\a")[0], "\\a");
assert(/\c0/.exec("\\c0")[0], "\\c0");

a = /(\.(?=com|org)|\/)/.exec("ah.com");
assert(a.index === 2 && a[0] === ".");

a = /(\.(?!com|org)|\/)/.exec("ah.com");
assert(a, null);

a = /(?=(a+))/.exec("baaabac");
assert(a.index === 1 && a[0] === "" && a[1] === "aaa");

a = /(z)((a+)?(b+)?(c))*/.exec("zaacbbbcac");
assert(a, ["zaacbbbcac", "z", "ac", "a", , "c"]);

a = eval("/\0a/");
assert(a.toString(), "/\0a/");
assert(a.exec("\0a")[0], "\0a");

assert(/{1a}/.toString(), "/{1a}/");
a = /a{1+/.exec("a{11");
assert(a, ["a{11"]);

/* test zero length matches */
a = /(?:(?=(abc)))a/.exec("abc");
assert(a, ["a", "abc"]);
a = /(?:(?=(abc)))?a/.exec("abc");
assert(a, ["a", undefined]);
a = /(?:(?=(abc))){0,2}a/.exec("abc");
assert(a, ["a", undefined]);
a = /(?:|[\w])+([0-9])/.exec("123a23");
assert(a, ["123a23", "3"]);

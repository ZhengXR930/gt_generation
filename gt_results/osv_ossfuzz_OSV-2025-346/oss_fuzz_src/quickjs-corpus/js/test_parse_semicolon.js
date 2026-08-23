/* 'yield' or 'await' may not be considered as a token if the
   previous ';' is missing */
function* f() {
    function func() {
    }

    yield 1;
    var h = x => x + 1
    yield 2;
}

async function g() {
    function func() {
    }

    await 1;
    var h = x => x + 1
    await 2;
}

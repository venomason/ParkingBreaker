from slug import slug

assert slug('Hello World') == 'hello-world'
assert slug('  Hello   World  ') == 'hello-world'
assert slug('Hello\tWorld\nAgain') == 'hello-world-again'
assert slug('') == ''
print('4 trusted cases passed')

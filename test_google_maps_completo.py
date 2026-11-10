import sys, requests
sys.path.insert(0, 'src')
import geocoder

r = geocoder.cruzar_calles('Avenida Brasil', 'Avenida Haiti', 'San Pedro de Jujuy')
print('a)', r)
assert abs(r['lat'] + 24.24225) < 0.01 and abs(r['lon'] + 64.87249) < 0.01, r
assert r['fuente'] == 'google-maps', r['fuente']

r = geocoder.cruzar_calles('RP 1', 'RP 41', 'San Pedro de Jujuy')
print('b)', r)
assert [r['lat'], r['lon']] == [-24.39366, -64.84887], r
print('TODOS LOS TESTS GEOCODER PASARON')

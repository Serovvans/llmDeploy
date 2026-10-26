# Библиотеки клиента CoGIS для стенда

Открытые выпуски вместо файлов клиента CoGIS заказчика (docs/portal-api.md §7.5.16, А-129):
compose стенда кладёт четыре файла в `client/` тома индекса SDK, если нет
`deploy/cogis-sdk/client/`. Лицензии — в `licenses/`.

| Файл | Выпуск | Из пакета npm | Лицензия |
|---|---|---|---|
| `jquery.js` | jQuery 3.7.1 | `jquery-3.7.1.tgz`, `dist/jquery.min.js` | MIT |
| `knockout.js` | Knockout 3.5.1 | `knockout-3.5.1.tgz`, `build/output/knockout-latest.js` | MIT |
| `leaflet.js` | Leaflet 1.9.4 | `leaflet-1.9.4.tgz`, `dist/leaflet.js` | BSD-2-Clause |
| `leaflet.css` | Leaflet 1.9.4 | `leaflet-1.9.4.tgz`, `dist/leaflet.css` | BSD-2-Clause |

Пакеты взяты с registry.npmjs.org; SHA-512 архивов сверены с полем `dist.integrity`
реестра:

- `jquery-3.7.1.tgz` — `sha512-m4avr8yL8kmFN8psrbFFFmB/If14iN5o9nw/NgnnM+kybDJpRsAynV2BsfpTYrTRysYUdADVD7CkUUizgkpLfg==`
- `knockout-3.5.1.tgz` — `sha512-wRJ9I4az0QcsH7A4v4l0enUpkS++MBx0BnL/68KaLzJg7x1qmbjSlwEoCNol7KTYZ+pmtI7Eh2J0Nu6/2Z5J/Q==`
- `leaflet-1.9.4.tgz` — `sha512-nxS1ynzJOmOlHp+iL3FyWqK89GtNL8U8rvlMOsQdTTssxZwCXh8N2NB3GDQOL+YR3XnWyZAxwQixURb+FA74PA==`

Картинки Leaflet (`dist/images/`) не входят: маршрут портала отдаёт только четыре файла.

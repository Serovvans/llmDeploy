# CoGIS: JS-плагины, HTML-кастомизация, GPTool — выжимка из документации заказчика

Источники и обозначения (стр. = номер страницы PDF; в скобках печатный номер, если нужен):
- **ПП** — «CoGIS версия 13 - Описание платформы (1).pdf», 41 стр. («CoGIS - Описание платформы.pdf» — тот же текст, проверено сравнением).
- **РП2** — «Руководство по созданию картографических приложений -часть2.pdf», 191 стр. Это фрагмент большого руководства: PDF-стр. 1 = печатная 499, печатный номер = PDF + 499. Начинается с п. 7.8.2.6 (отчёты), дальше п. 7.9–7.11, 8 (Правила SOE).
- **СБ** — «CoGIS - Используемые сторонние библиотеки.pdf», 7 стр.
- **РП-QGIS** — «Руководство по созданию картографических проектов (1).pdf», 138 стр. Это руководство по QGS-проектам (БД, слои, стили, геокодирование, вложения, печать). JS, HTML, виджетов и плагинов в нём **нет**.

## Что есть в наборе и чего нет (главное)

- Раздела **7.7 «Статистика, виджеты и плагины»** (настройка виджета, блоки «JavaScript-плагин» и «HTML», элемент «вызов JavaScript-кода» в карточке объекта) в предоставленных файлах **нет**: РП2 начинается с п. 7.8. Сохранились только ссылки на него (РП2, стр. 36, 38, 60).
- Формат JS-плагина виджета (один файл или код в поле, сигнатура, жизненный цикл) **не описан**. Известно лишь, что API (п. 7.10) доступен «в виджете в JavaScript-плагине» и «в карточке объекта в элементе вызова JavaScript-кода» (РП2, стр. 66).
- Документации на GPTool (C#, .NET 8, интерфейсы, сборка, регистрация) и Python-инструментов **нет**. Версий .NET-пакетов нет, кроме трёх (см. §5).

## 1. JavaScript-плагины

- Платформа даёт два «простых способа встраивания» внешних элементов в виде виджетов: (а) на основе JavaScript-плагинов, «которые могут вызывать любой внешний инструмент или сервис»; (б) HTML-код для справочной информации (ПП, стр. 28, п. 3.4; также стр. 27).
- В Конструкторе на карту добавляются: виджет статистики (графики, диаграммы, показатели), справочная информация в форме HTML-кода, таблица, страница со ссылкой; кнопка перехода на произвольную веб-страницу; «**JavaScript-плагин с дополнительной функциональностью, который будет активироваться нажатием на кнопку**» (ПП, стр. 12).
- Вид отображения виджета: «Окно» или «Панель» (РП2, стр. 38, ссылка на п. 7.7.2.1.1). Есть блок виджета «Генерация отчета» (РП2, стр. 60).
- Глобальный объект платформы — **`api`**. Клиент использует Knockout (в примерах `data-bind="click: ..."`, `ko.observable`) и jQuery (`$($element).val()`) (РП2, стр. 64–69; СБ, стр. 1).
- Отдельный механизм: файл HTML в папке `\Customer\openProject\` на сервере портала, в нём глобальные функции-хуки на `window` (РП2, стр. 64–66, п. 7.9.3–7.9.4). Это не виджет, а настройка карточки объекта; требует доступа к серверу.

### Функции `api` (РП2, стр. 66–68, п. 7.10)
1. Карта: `createExtent(xmin,xmax,ymin,ymax,sr)`, `createPoint(x,y,sr)`, `getCentroid(geometry)`, `getSpatialReference(id)` (wkid или wkt), `project(geometry, sr)`, `goTo(geometry, maxScale, minScale, marginInPercent=20)`, `highlight(geometry, withLines=false)`, `showPushpin(geometry, icon)`.
2. Логика проекта: `getLayer(serviceUrl, layerId, existsIn=map/legend/search/attributeTable)`, `searchInLayer(layer, whereClause, objectIds, geometry)` (объекты с геометрией и атрибутами, СК = СК карты, если запросы разрешены настройками), `geocodeAddress(address, filters={...})` (используется первый геокодер проекта), `addFilterToLayer(layer, criterion)`, `removeFilterInLayer(layer, filter)`, `clearAllFiltersInLayer(layer)`.
3. Визуальные: `search(text)`, `openIdentify(feature, goTo=true, calloutOnly=false)`, `identify(geometry)`, `openAttributeTable(layer, panelHeight)` (пикселы или строка «30%»), `openWidget(name)`, `showAlert("Заголовок", "Сообщение")`.
4. Сетевой анализ и графика: `api.constructServiceArea(points, cost, breaks, restrictions)`, `api.findRoute(points, cost, restrictions)`, `api.addGraphic(geometryInWGS84, symbol)`, `api.deleteGraphic(graphic)`, `api.createColor(r,g,b,alpha)`, `api.createSimplePointSymbol(color,size,outline,type)`, `api.createPicturePointSymbol(icon,iconSize,locationX,locationY)`, `api.createSimpleLineSymbol(color,width,type)`, `api.createSimpleFillSymbol(color,outline,type)`. Очистка маршрута или зон — вызов с пустым массивом точек.
5. Только в примерах, не в списке: `api.addAttributeFilterToLayer(layer, field, [values], 'Equal')`, `api.removeAttributeFilterToLayer(layer, field, [values], 'Equal')`, `api.generateReport(template, {serviceUrl, layerId, whereClause, orderBy}, {Caption})` (РП2, стр. 68–69).

### Примеры (дословно, РП2, стр. 66–69)
```
api.constructServiceArea([api.createPoint(82.9246, 55.0304, api.getSpatialReference(4326))], 'Geodesic_Length', [3, 6], ['Car'])
api.findRoute([api.createPoint(82.9246, 55.0304, api.getSpatialReference(4326)), api.createPoint(82.9546, 55.4304, api.getSpatialReference(4326))], 'Geodesic_Length', ['Car'])
api.createSimplePointSymbol(api.createColor(255, 2, 2), 10);
```
```
<script>
api.openAttributeTable(api.getLayer("https://.../MapServer", 0),300);
</script>
<script>
var point = api.createPoint(7414817.879000001, 1.0027362619900003E7, api.getSpatialReference(3857));
api.goTo(point);
api.identify(point);
</script>
<script>api.search('Администрация');</script>
<script>
api.generateReport("simple.xlsx", {serviceUrl: "https://.../MapServer", layerId: 4, whereClause:"", orderBy:""}, {Caption: "test"});
</script>
```
Фильтр с полем ввода (стр. 68–69): `<input id='filtrationExample' ... data-bind="event: { keypress: function (data, event) { if (event.keyCode == 13) { api.addAttributeFilterToLayer(api.getLayer('https://.../MapServer' , 0), 'status', [$($element).val()], 'Equal'); return false; } return true; } }" />` и `<button class='action' data-bind="click: function() { var layer = api.getLayer('https://.../MapServer' , 0); api.removeAttributeFilterToLayer(layer, 'status', [$('#filtrationExample').val()], 'Equal') }">Удалить фильтр</button>`.

Ограничения: `getLayer` ищет слой по URL сервиса и id, значит нужен реальный проект CoGIS. Песочница, CSP, допустимые домены, поддерживаемый стандарт JS (ES5/ES6) **не описаны**.

## 2. HTML-виджеты и HTML-кастомизация

### HTML-виджет
- Виджет «справочная информация в форме HTML-кода» (ПП, стр. 12, 28). Из примеров п. 7.10 видно, что в HTML-блоке работают `<script>`, `data-bind` (Knockout), jQuery, CSS-класс `action` у кнопки (РП2, стр. 68–69). Явного описания блока в наборе нет.
- HTML допускается и в описании параметров отчёта (РП2, стр. 38), и в тексте сообщений геотриггеров (стр. 122–125).

### Вкладка «HTML-кастомизация» (РП2, стр. 61–63, п. 7.9)
- Где: для папок и карт Каталога карт: вкладка «HTML-кастомизация» → «Добавить кастомизацию».
- Типы: **HTML, CSS, JavaScript**. Для CSS и JS обрамляющие теги не нужны, «при компоновке страницы теги добавятся автоматически»; код вставляется «как есть» (стр. 62).
- Настройки: применение по иерархии (текущая папка / дочерние: папки, карты, страницы; только для папок), по типу клиента (смартфон, планшет, настольный ПК; по умолчанию все), **место вставки** (начало `<head>`, конец `<head>`, начало `<body>`, конец `<body>`), поле «Код». Инструменты: экспорт в XML, временно отключить, удалить.
- К элементу Каталога можно обращаться по его полю ID (в примере `data-block-id="Folder_2"`).
- Доступ к `api` карты из кастомизации Каталога не описан.

Примеры (дословно):
```
.catalog-items-container:nth-child(2n+1) { background-color: lemonchiffon; }
.catalog-items-container[data-block-id="Folder_2"] { background-color: yellowgreen; }
```
(стр. 63, п. 7.9.1: фон блоков Каталога карт)
```
function onCesiumKmlInit(kmlLayer) {
  kmlLayer.entities.values.forEach(x => {
    if (x.label) x.label.show = false;
    if (x.polyline) x.polyline.show = false;
    if (x.billboard) x.billboard.show = false;
  });
}
```
(стр. 63–64, п. 7.9.2: скрытие меток в 3D KML; хук `onCesiumKmlInit`, значит 3D построен на Cesium.)

### Хуки карточки объекта (РП2, стр. 64–66)
- `window.checkBeforeSave = function (object, project) { ... return ko.observable(список_ошибок) }`: проверка перед «Сохранить». Используются `object.attributes().keyValues.first(...)`, `object.service.isApplicable(url)`, `object.id.layerId`, `errors.addArray`, `errors.where`.
- `window.checkButtonVisibilityInCard = function (object, button, project)`: возвращает `ko.observable(true|false)`; `button.caption` — подпись кнопки.
- `window.checkBeforeExecuteButtonInCard = function (object, button, project)`: аналогично, перед запуском задачи; пример использует `object.attributes().get("Район")()` и `api.showAlert("Заголовок", "Ошибка")`.

## 3. Встраивание внешних инструментов, вызов GPTool из JS

- ПП, стр. 12, 27–28: JS-плагин «может вызывать любой внешний инструмент или сервис»; кнопка перехода на веб-страницу; страница со ссылкой. **iframe в документах не упоминается.** Механизм вызова (fetch, ajax, обёртка) не описан.
- Кнопки запуска инструментов геообработки — штатный элемент приложения (ПП, стр. 27); в карточке объекта есть кнопка «Вызов задачи сервиса геообработки» (РП2, стр. 65).
- В `api` **нет функции вызова геообработки**. Сервисы геообработки публикуются «с доступом по REST API» (ПП, стр. 20). В СБ есть `esri-leaflet-gp-debug.js`, то есть клиент умеет вызывать ArcGIS-совместимые GP-задачи (это вывод, не утверждение документации). Формат запроса из JS не описан.
- Интеграция с внешними системами: HTTP/HTTPS, RESTful (ПП, стр. 28).

## 4. Геообработка (коротко)

- Сервисы геообработки: модели из визуального редактора, более 60 готовых инструментов, REST API (ПП, стр. 20–21, п. 2.6.2). Типы параметров: Int, Double, Bool, String, Date-Time, File, Geometry, Coordinate System, SQL-expression, Workspace, Table, Standalone Table, Feature Class, Feature Layer, Map, Image/Tile/Dynamic Service. Описание инструментов — в «Руководстве по публикации ГИС-сервисов» (в наборе нет).
- Геотриггер «Вызов задачи геообработки» (РП2, стр. 140–147): URL сервиса, логин/пароль, task, входные параметры (поля и макросы), обновляемые поля по выходным параметрам (`{OutputFeatureLayer.N.myfield}`, `{prop.N.prop1.prop2}`), таймаут; время запуска: до обновления, после обновления, после транзакции.
- Геотриггер «**Вызов .NET метода**» (РП2, стр. 147–152): путь до dll-файла .NET библиотеки, полное имя класса с namespace, имя статического метода, аргументы, поле для записи, таймаут. Единственное в наборе описание подключения C#-кода; сигнатура метода (типы, возврат) **не описана**.
- Отчёты: предобработка «Вызов DLL-библиотеки» (DLL регистрируется в «Администрирование > Настройки > Сторонние обработчики», РП2, стр. 48); постобработка «Внешний процесс» (путь к exe, макросы `{ReportFolder}`, `{ReportName}`, `{ReportExtFile}`, таймаут, очередь по группе; стр. 53–55) и «Инструмент геообработки» с макросами `{ReportPath}`, `{Source}` (JSON), `{Source.Attachments}` (zip), `{Map}` (png) (стр. 57–59, таблица 15).
- GPTool как термин, .NET 8, Python-инструменты в документах **не встречаются**. Платформа: «.NET Core (C#), ASP.NET Core Web Application и HTML/JavaScript», без номера версии (ПП, стр. 29).

## 5. Сторонние библиотеки (СБ, стр. 1–7)

Версий в документе почти нет. Явно указаны только в URL NuGet: **Shorthand.ImageSharp.WebP 2.2.1** (портал), **Docnet.Core 2.3.1** и **MetadataExtractor 2.7.1** (ГИС-сервер).

Клиент и веб-часть портала:
- Картография: **Leaflet** (leaflet.js), **esri-leaflet**, esri-leaflet-gp-debug, esri-leaflet-renderers-debug, leaflet.draw, leaflet-freedraw, Leaflet.Editable, Leaflet.Snap, Path.Drag, L.Pather, leaflet.bing, leaflet.yandex, Leaflet.GoogleMutant, proj4 / proj4leaflet, Leaflet.UTM. **OpenLayers в списке нет.** Cesium в списке нет, но хук `onCesiumKmlInit` указывает на него.
- UI и утилиты: **jQuery**, jQuery UI, jquery-ui-timepicker-addon, blueimp-gallery, jquery.iframe-transport, resizable-table-column, **Knockout**, Foundation (ZURB), **Chart.js**, svg.js, svg4everybody, react-app-polyfill.
- .NET-часть портала: .NET Core, DocumentFormat.OpenXml, **Newtonsoft.Json**, MailKit, MimeKit, FastMember.

ГИС-сервер (C#-слой): .NET Core, DocumentFormat.OpenXml, Newtonsoft.Json, MailKit, MimeKit, Docnet.Core, PDFSharp, MetadataExtractor; React-стек (react, redux, antd, material-ui, konva, axios, lodash, moment, i18next и др.) без версий. Ядро на C++: JsonCpp, Clipper, OGR (ссылка на gdal.org/1.11), Expat, Freetype, GeoTIFF, ICU, libjpeg, **LibPQ**, OpenSSL, libpng, Proj4, Protozero, SQLite, libtiff, WebP, zlib.

**Нет в списке:** Npgsql, NetTopologySuite, GDAL/OGR для .NET, Python-библиотек. Доступ к PostgreSQL в ядре идёт через LibPQ (C++). Версии нужно запросить у заказчика (или снять с работающего экземпляра портала); в этих PDF их нет.

## 6. Выводы для дизайна раздела «Код» 1.0

Шаблоны, которые покрываются документацией:
1. Кнопка-сценарий с `api`: поиск, приближение с идентификацией, подсветка и метка, открытие таблицы.
2. Фильтр слоя по полю ввода (HTML + Knockout `data-bind` + `addAttributeFilterToLayer`) — готовый пример (РП2, стр. 68–69).
3. Маршрут и зона доступности (`findRoute`, `constructServiceArea`).
4. Графика на карте (`addGraphic`, символы, `createColor`).
5. Генерация отчёта (`api.generateReport`).
6. HTML-кастомизация Каталога карт: CSS по `data-block-id`, место вставки head/body как параметр шаблона.
7. Справочный HTML-виджет (текст, таблица, ссылки).
8. Хуки карточки объекта (`checkBeforeSave` и др.) — по необходимости, требуют доступа к серверной папке.

Что можно показать в предпросмотре без сервера CoGIS (фрейм с `sandbox`, подмена `api`):
- Вёрстку, CSS (на тестовом DOM с `.catalog-items-container` и `data-block-id`), синтаксис JS, ошибки консоли.
- **Заглушка `api`** с теми же именами функций: пишет вызовы в журнал («вызван `goTo` с точкой X»), `showAlert` показывает окно, `createPoint`/`createColor` возвращают простые объекты; слои и объекты фиктивные. Карта в предпросмотре — упрощённая (холст или Leaflet с офлайн-основой); требования к ней в документах не заданы.
- jQuery и Knockout нужно подключить в предпросмотр, иначе примеры с `data-bind` и `$($element)` не заработают (версии неизвестны).

Что проверить без сервера нельзя: реальные `getLayer`, `searchInLayer`, `geocodeAddress`, `findRoute`, `constructServiceArea`, `generateReport`, фильтры, идентификацию и карточку объекта; поведение по типам клиентов (только размер фрейма); Cesium/3D; права доступа; хуки в `\Customer\openProject\`.

Пробелы для запроса у заказчика:
- Раздел 7.7 (блоки виджета, формат JS-плагина) и один рабочий пример плагина.
- Версии Leaflet, jQuery, Knockout, Newtonsoft.Json и состав SDK для GPTool (интерфейс, пакет, пример проекта .NET 8); Npgsql/NetTopologySuite в документации не упомянуты.
- Типы и возвраты функций `api`; подсказки модели ограничить перечнем из §1 и запретить выдумывать другие функции.
- Расхождения в самой документации: в списке `addFilterToLayer`, в примере `addAttributeFilterToLayer`; `createSimpleLineSymbol` перечислен дважды (стр. 67–68), вероятно, вторая строка должна описывать другой символ.

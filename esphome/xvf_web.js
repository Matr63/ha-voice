// The board's web page: a hint under each setting, the page's own titles in Russian.
// The page comes from oi.esphome.io and has no place for descriptions; this only
// adds text to it. Keys are the entity names from ha-voice-xvf3800.yaml.
(function () {
  var HINTS = {
    "Микрофон": "Выключено: микрофоны отключены в самом чипе, как кнопкой Mute на плате. На кольце видны красные метки.",
    "Слушать": "Отправлять ли звук на сервер HA Voice. Выключено: колонка не реагирует на команды и не пишет дневник.",
    "Автоусиление речи AGC: максимум": "Насколько чип может поднять тихую речь в канале речи. Больше: лучше слышно издалека, но громче и фон. Значение из панели HA Voice важнее этого.",
    "Усиление ключевого слова ASR": "Громкость канала, который слушает детектор ключевого слова. Больше: срабатывает издалека, но может чаще ошибаться. Панель HA Voice задаёт его заново при каждом подключении, поэтому меняйте его там.",
    "Маршрут каналов DSP": "Какой канал чипа идёт на распознавание речи. Обычно лучше с шумоподавлением. Ключевое слово всегда слушает канал ASR.",
    "Перезагрузить плату": "Перезапускает плату. Настройки и Wi-Fi сохраняются.",
    "Сервер HA Voice": "Адрес сервера с дополнением HA Voice. Меняйте, если плата не может его найти.",
    "Прошивка чипа XVF3800": "Версия прошивки самого аудиочипа. Это не версия прошивки платы; чип плата обновляет сама.",
    "Состояние чипа DSP": "OK: чип отвечает. Иначе см. раздел про XVF3800 в инструкции HA Voice.",
    "Связь с HA Voice": "Подключена ли плата к серверу. Без связи колонка не слышит команды и не отвечает.",
    "Сигнал Wi-Fi": "Чем ближе к нулю, тем лучше. Примерно до −67 дБм связь хорошая, ниже −80 дБм может прерываться."
  };
  var TITLES = { "OTA Update": "Обновление прошивки", "Debug Log": "Журнал", "Update": "Загрузить", "PRESS": "Нажать" };

  function retitle(root) {
    root.querySelectorAll(".tab-header, .abutton").forEach(function (el) {
      if (TITLES[el.textContent]) el.textContent = TITLES[el.textContent];
    });
    root.querySelectorAll("input[type=submit]").forEach(function (el) {
      if (TITLES[el.value]) el.value = TITLES[el.value];
    });
  }

  function hints(root) {
    root.querySelectorAll(".entity-row").forEach(function (row) {
      var label = row.children[1];
      if (!label || label.querySelector("small")) return;
      var text = HINTS[label.textContent.trim()];
      if (!text) return;
      var hint = document.createElement("small");
      hint.textContent = text;
      hint.style.cssText = "display:block;font-size:.8em;line-height:1.3;opacity:.65;font-weight:normal;margin-top:2px;white-space:normal";
      label.appendChild(hint);
    });
  }

  // The page draws itself after this script and redraws rows when entities change.
  setInterval(function () {
    var app = document.querySelector("esp-app");
    if (!app || !app.shadowRoot) return;
    retitle(app.shadowRoot);
    var table = app.shadowRoot.querySelector("esp-entity-table");
    if (table && table.shadowRoot) {
      retitle(table.shadowRoot);
      hints(table.shadowRoot);
    }
    var log = app.shadowRoot.querySelector("esp-log");
    if (log && log.shadowRoot) retitle(log.shadowRoot);
  }, 1000);
})();

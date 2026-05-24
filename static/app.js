const tabs = document.querySelectorAll(".tab");
const panels = document.querySelectorAll(".panel");
const sourceType = document.getElementById("source-type");
const citationStyle = document.getElementById("citation-style");
const formatForm = document.getElementById("format-form");
const formattedOutput = document.getElementById("formatted-output");
const rawInput = document.getElementById("raw-input");
const parsedOutput = document.getElementById("parsed-output");
const parseButton = document.getElementById("parse-button");
const parseMeta = document.getElementById("parse-meta");
const copyButton = document.getElementById("copy-output");
const recognizedFields = document.getElementById("recognized-fields");

const fieldLabels = {
  source_type: "Тип источника",
  citation_style: "Стиль оформления",
  authors: "Авторы",
  title: "Название",
  subtitle: "Подзаголовок",
  translator: "Переводчик",
  translation_from: "Язык оригинала",
  city: "Город",
  publisher: "Издательство",
  year: "Год",
  pages: "Страницы",
  isbn: "ISBN",
  journal: "Журнал",
  volume: "Том / выпуск",
  issue: "Номер",
  issn: "ISSN",
  doi: "DOI",
  site_name: "Название сайта",
  url: "URL",
  access_date: "Дата обращения",
  confidence: "Уверенность",
  notes: "Комментарий",
};

function updateDynamicLabels() {
  const issueLabel = formatForm.querySelector('[data-field-label="issue"]');
  const issueInput = formatForm.querySelector('input[name="issue"]');

  if (issueLabel) {
    issueLabel.textContent = "Номер";
  }

  if (issueInput) {
    issueInput.placeholder = "3";
  }
}

tabs.forEach((tab) => {
  tab.addEventListener("click", () => {
    tabs.forEach((item) => item.classList.remove("tab--active"));
    panels.forEach((panel) => panel.classList.remove("panel--active"));
    tab.classList.add("tab--active");
    document.getElementById(tab.dataset.tab).classList.add("panel--active");
  });
});

function updateVisibleFields() {
  const kind = sourceType.value;
  const activeNames = new Set();

  document.querySelectorAll("#format-form label").forEach((field) => {
    field.classList.remove("is-active", "is-inactive");
  });

  document.querySelectorAll("[data-common]").forEach((field) => {
    field.classList.add("is-active");
    field.querySelectorAll("input, select, textarea").forEach((input) => {
      input.disabled = false;
      activeNames.add(input.name);
    });
  });

  document.querySelectorAll("[data-kind]").forEach((field) => {
    const kinds = field.dataset.kind.split(" ");
    const isVisible = kinds.includes(kind);
    field.hidden = !isVisible;
    field.classList.add(isVisible ? "is-active" : "is-inactive");
    field.querySelectorAll("input, select, textarea").forEach((input) => {
      if (!isVisible) {
        input.value = "";
      } else {
        activeNames.add(input.name);
      }
      input.disabled = !isVisible;
    });
  });

  formatForm.querySelectorAll("input, select, textarea").forEach((input) => {
    if (!activeNames.has(input.name) && input.name !== "source_type") {
      input.value = "";
    }
  });

  updateDynamicLabels();
}

async function formatReference() {
  const formData = new FormData(formatForm);
  const payload = Object.fromEntries(formData.entries());
  payload.citation_style = citationStyle.value;

  const response = await fetch("/api/format", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });

  const data = await response.json();
  formattedOutput.value = data.formatted || data.error || "";
}

async function parseReferences() {
  parseButton.disabled = true;
  parseButton.textContent = "Разбираю...";
  parseMeta.innerHTML = "";
  recognizedFields.innerHTML = "";

  try {
    const response = await fetch("/api/parse", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        raw_text: rawInput.value,
        citation_style: citationStyle.value,
      }),
    });

    const data = await response.json();

    if (!response.ok) {
      parsedOutput.value = data.error || "Не удалось обработать ссылки.";
      return;
    }

    const lines = (data.results || []).map((item) => item.formatted);
    parsedOutput.value = lines.join("\n");

    (data.results || []).forEach((item, index) => {
      const row = document.createElement("div");
      const errorPart = item.parser_error ? ` Ошибка модели: ${item.parser_error}` : "";
      row.textContent = `${index + 1}. Тип: ${item.detected_type}. Режим: ${item.parser_used}. ${item.fields.notes || ""}${errorPart}`;
      parseMeta.appendChild(row);
      recognizedFields.appendChild(buildFieldCard(item, index));
    });
  } catch (error) {
    parsedOutput.value = "Не удалось связаться с сервером.";
  } finally {
    parseButton.disabled = false;
    parseButton.textContent = "Разобрать";
  }
}

function buildFieldCard(item, index) {
  const card = document.createElement("section");
  card.className = "field-card";

  const top = document.createElement("div");
  top.className = "field-card__top";

  const title = document.createElement("h3");
  title.className = "field-card__title";
  title.textContent = `Распознанная запись ${index + 1}`;

  const button = document.createElement("button");
  button.type = "button";
  button.className = "ghost-button";
  button.textContent = "Перенести в конструктор";
  button.addEventListener("click", async () => {
    await transferToConstructor(item.fields);
  });

  top.appendChild(title);
  top.appendChild(button);
  card.appendChild(top);

  const grid = document.createElement("div");
  grid.className = "field-grid";

  Object.entries(item.fields).forEach(([key, value]) => {
    if (!value) {
      return;
    }
    const field = document.createElement("div");
    field.className = "field-item";
    field.innerHTML = `<strong>${fieldLabels[key] || key}</strong><span>${value}</span>`;
    grid.appendChild(field);
  });

  if (!grid.children.length) {
    const field = document.createElement("div");
    field.className = "field-item";
    field.innerHTML = "<strong>Поля</strong><span>Модель не смогла извлечь данные.</span>";
    grid.appendChild(field);
  }

  card.appendChild(grid);
  return card;
}

async function transferToConstructor(fields) {
  const sourceTypeValue = fields.source_type || "book";

  if (fields.citation_style) {
    citationStyle.value = fields.citation_style;
  }

  sourceType.value = sourceTypeValue;
  updateVisibleFields();

  Object.entries(fields).forEach(([key, value]) => {
    const input = formatForm.querySelector(`[name="${key}"]`);
    if (input && !input.disabled) {
      input.value = value || "";
    }
  });

  tabs.forEach((item) => item.classList.remove("tab--active"));
  panels.forEach((panel) => panel.classList.remove("panel--active"));
  document.querySelector('[data-tab="constructor"]').classList.add("tab--active");
  document.getElementById("constructor").classList.add("panel--active");

  await formatReference();
}

sourceType.addEventListener("change", async () => {
  updateVisibleFields();
  await formatReference();
});

citationStyle.addEventListener("change", async () => {
  await formatReference();
});

formatForm.addEventListener("input", async () => {
  await formatReference();
});

parseButton.addEventListener("click", parseReferences);

copyButton.addEventListener("click", async () => {
  await navigator.clipboard.writeText(formattedOutput.value);
  copyButton.textContent = "Скопировано";
  setTimeout(() => {
    copyButton.textContent = "Скопировать";
  }, 1200);
});

updateVisibleFields();
formatReference();

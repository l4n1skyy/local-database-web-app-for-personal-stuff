// The app pages: chart tooltips, the word lists (search, filters, quiz mode,
// moving words without a page reload) and the workout log form.

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

// --- Chart tooltips: any element with data-tip-v (value) and data-tip-l (label)

const tip = $("#tip");

function showTip(el, x, y) {
  tip.querySelector("b").textContent = el.dataset.tipV;
  tip.querySelector("span").textContent = el.dataset.tipL;
  tip.hidden = false;
  const r = tip.getBoundingClientRect();
  tip.style.left = Math.min(x + 14, innerWidth - r.width - 8) + "px";
  tip.style.top = Math.max(8, y - r.height - 12) + "px";
}

document.addEventListener("pointermove", (e) => {
  const el = e.target.closest?.("[data-tip-v]");
  if (el) showTip(el, e.clientX, e.clientY);
  else tip.hidden = true;
});
document.addEventListener("focusin", (e) => {
  const el = e.target.closest?.("[data-tip-v]");
  if (!el) return;
  const r = el.getBoundingClientRect();
  showTip(el, r.left + r.width / 2, r.top);
});
document.addEventListener("focusout", () => (tip.hidden = true));

// --- Chinese word lists ------------------------------------------------------------

const words = $("#words");

if (words) {
  const q = $("#q"), hsk = $("#f-hsk"), cat = $("#f-cat"), hide = $("#hide");
  const plain = (s) => s.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();

  function visibleCards() {
    return $$(".word", words).filter((c) => !c.hidden);
  }

  function updateShown() {
    const n = visibleCards().length;
    $("#shown").textContent = `${n} word${n === 1 ? "" : "s"}`;
    $("#none").hidden = n > 0;
  }

  function filter() {
    const text = plain(q.value.trim());
    for (const card of $$(".word", words)) {
      card.hidden =
        (text && !plain(card.dataset.text).includes(text)) ||
        (hsk && hsk.value && card.dataset.hsk !== hsk.value) ||
        (cat && cat.value && card.dataset.cat !== cat.value);
    }
    updateShown();
  }
  q.addEventListener("input", filter);
  hsk?.addEventListener("change", filter);
  cat?.addEventListener("change", filter);

  // Quiz mode is remembered per browser
  try {
    hide.checked = localStorage.getItem("hideMeaning") === "1";
  } catch {}
  words.classList.toggle("quiz", hide.checked);
  hide.addEventListener("change", () => {
    words.classList.toggle("quiz", hide.checked);
    try {
      localStorage.setItem("hideMeaning", hide.checked ? "1" : "0");
    } catch {}
  });
  words.addEventListener("click", (e) => {
    const card = e.target.closest(".word");
    if (card && words.classList.contains("quiz") && !e.target.closest("button, a")) card.classList.toggle("shown");
  });

  async function move(ids, to) {
    const body = new URLSearchParams({ ids: ids.join(","), to });
    const res = await fetch("/-/chinese/move", { method: "POST", body, headers: { Accept: "application/json" } });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error);
    for (const [status, n] of Object.entries(data.counts)) {
      const el = $(`[data-count="${status}"]`);
      if (el) el.textContent = n;
    }
    for (const id of ids) {
      const card = $(`.word[data-id="${id}"]`, words);
      card.classList.add("leaving");
      setTimeout(() => {
        card.remove();
        updateShown();
      }, 200);
    }
  }

  // Learnt / Forgot buttons: move the word without reloading the page
  words.addEventListener("submit", (e) => {
    e.preventDefault();
    const form = e.target, button = e.submitter;
    $$("button", form).forEach((b) => (b.disabled = true));
    move([form.elements.namedItem("id").value], button.value).catch(() => form.submit());
  });

  $("#all-learnt")?.addEventListener("click", async () => {
    const ids = visibleCards().map((c) => c.dataset.id);
    if (!ids.length || !confirm(`Mark all ${ids.length} shown word${ids.length === 1 ? "" : "s"} as learnt?`)) return;
    await move(ids, "learnt");
  });
}

// --- Workout log ---------------------------------------------------------------------

const exercise = $("#exercise");

if (exercise) {
  const last = JSON.parse($("#last-sets").textContent);
  // Picking a known exercise fills in what you did last time
  exercise.addEventListener("change", () => {
    const found = last.find((s) => s.exercise.toLowerCase() === exercise.value.trim().toLowerCase());
    if (!found) return;
    $("#reps").value = found.reps;
    $("#weight").value = found.weight;
  });
  for (const form of $$("form.delete")) {
    form.addEventListener("submit", (e) => {
      if (!confirm("Delete this set?")) e.preventDefault();
    });
  }
  // After logging, keep the cursor ready for the next set
  if (new URLSearchParams(location.search).has("msg")) $("#reps").select();
}

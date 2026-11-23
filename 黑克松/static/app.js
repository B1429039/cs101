(() => {
  const search = document.querySelector("[data-search]");
  const buttons = [...document.querySelectorAll("[data-filter]")];
  const cards = [...document.querySelectorAll("[data-post]")];
  if (!search || !cards.length) return;
  let filter = "all";
  const update = () => {
    const query = search.value.trim().toLowerCase();
    let visible = 0;
    cards.forEach(card => {
      const matchesFilter = filter === "all" || card.dataset.type === filter;
      const matchesQuery = !query || card.textContent.toLowerCase().includes(query);
      const show = matchesFilter && matchesQuery;
      card.hidden = !show;
      if (show) visible += 1;
    });
    const empty = document.querySelector("[data-filter-empty]");
    if (empty) empty.hidden = visible !== 0;
  };
  search.addEventListener("input", update);
  buttons.forEach(button => button.addEventListener("click", () => {
    filter = button.dataset.filter;
    buttons.forEach(item => item.classList.toggle("active", item === button));
    update();
  }));
})();


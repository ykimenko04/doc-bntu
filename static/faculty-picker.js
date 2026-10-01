const normalizeFacultyText = (value) => value.trim().toLocaleLowerCase('ru-RU');

const facultyMatchRank = (name, query) => {
  if (!query) return 0;
  if (name.startsWith(query)) return 0;
  if (name.split(/[\s,.-]+/u).some((word) => word.startsWith(query))) return 1;
  return Number.POSITIVE_INFINITY;
};

document.querySelectorAll('.faculty-multiselect').forEach((picker) => {
  const summary = picker.querySelector('.faculty-summary');
  const options = [...picker.querySelectorAll('[data-faculty-option]')];
  const inputs = options.map((option) => option.querySelector('input[type="checkbox"]'));
  const search = picker.querySelector('.faculty-search');
  const optionContainer = picker.querySelector('.faculty-options');

  options.forEach((option, index) => {
    option.dataset.originalOrder = String(index);
    option.dataset.facultyName = normalizeFacultyText(option.textContent);
  });

  const refreshSummary = () => {
    const selected = inputs.filter((input) => input.checked);
    if (!selected.length) summary.textContent = picker.dataset.optional ? 'Все факультеты' : 'Выберите факультеты';
    else if (selected.length === 1) summary.textContent = selected[0].value;
    else summary.textContent = `${selected[0].value} + ещё ${selected.length - 1}`;
  };

  const filterAndRank = () => {
    const query = normalizeFacultyText(search.value);
    const ranked = options
      .map((option) => ({ option, rank: facultyMatchRank(option.dataset.facultyName, query) }))
      .sort((left, right) => {
        if (left.rank !== right.rank) return left.rank - right.rank;
        return left.option.dataset.facultyName.localeCompare(
          right.option.dataset.facultyName,
          'ru-RU',
          { sensitivity: 'base' },
        );
      });

    ranked.forEach(({ option, rank }) => {
      option.hidden = !Number.isFinite(rank);
      optionContainer.append(option);
    });
  };

  inputs.forEach((input) => input.addEventListener('change', refreshSummary));
  search.addEventListener('input', filterAndRank);
  if (!picker.dataset.optional) {
    picker.closest('form').addEventListener('submit', (event) => {
      if (!inputs.some((input) => input.checked)) {
        event.preventDefault();
        picker.open = true;
        summary.textContent = 'Выберите хотя бы один факультет';
      }
    });
  }
  refreshSummary();
});

document.addEventListener('click', (event) => {
  document.querySelectorAll('.faculty-multiselect[open]').forEach((picker) => {
    if (!picker.contains(event.target)) picker.open = false;
  });
});

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') {
    document.querySelectorAll('.faculty-multiselect[open]').forEach((picker) => {
      picker.open = false;
    });
  }
});

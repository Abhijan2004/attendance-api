# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.** Which indexes did you create, and which query or guarantee does each one serve? Which one did you consider and reject?
ans : Unique (emp_code, date) on attendance_logs: one record per employee per day, and fast monthly lookups (explain confirmed it). An index on date for month-wide queries like the leaderboard and trend. Unique emp_code on employees for unique codes and quick joins. I skipped a separate emp_code index on logs, since the first covers it.
2. **Punch-in race.** Two identical punch-in requests arrive at the same instant. Walk through exactly what happens in your code.
ans:Both requests see no existing record, since neither has saved yet, and both try to insert. The unique index lets only one through; the other gets a DuplicateKeyError, which I return as a 409. One 201, one 409.
3. **Ties.** Two employees are tied on late minutes at the cutoff of `limit`. What does your leaderboard return, and why?
ans:It returns both. $rank gives tied people the same rank (1, 2, 2, 4) and I keep everyone within the limit, so the list can be longer than the limit. Cutting at a fixed position would drop one tied person unfairly.
4. **Headcount.** How does your department summary make sure employees with zero logs are counted?
ans:I start from employees, not logs, and join the logs on with $lookup, which acts like a left join. An employee with no logs keeps an empty list instead of disappearing, so they are still counted once.
5. **One thing you would change** if this had to serve 100x the data.
ans:I would keep pre-computed monthly totals, updated on each punch, instead of aggregating raw records per request, and store a real date field instead of parsing text dates.

// The scenes. Every number rendered here comes from demo/data/story.json, which was
// exported from recorded agent runs by demo/build_story.py. Nothing is computed from
// assumptions, and a value the recorded runs do not contain is shown as "—".

import { actorCard, character } from './characters.js';

const escape = (value) =>
  String(value ?? '').replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])
  );

const num = (value) => (value === null || value === undefined ? '—' : value.toLocaleString('en-US'));
const pct = (value) => (value === null || value === undefined ? '—' : `${Math.round(value * 100)}%`);

const says = (who, cls, text) =>
  `<div class="says ${cls}"><span class="who">${escape(who)}</span>${text}</div>`;

/** The paper the player is handed, chosen by a rule stated on screen. */
function playerTarget(data) {
  const modal = data.targets.filter((target) => {
    const rewrite = target.strategies.rewrite;
    const overlap = target.strategies.overlap;
    return rewrite && overlap && !overlap.ever_cited && rewrite.cited_count === rewrite.execution_count;
  });
  return (modal.length ? modal : data.targets)[0];
}

const strategyOf = (data, name) => data.strategies.find((s) => s.strategy === name);

export const scenes = [
  // ------------------------------------------------------------------ 1. title
  {
    id: 'title',
    autoplayMs: 2600,
    render(data) {
      return `
<div class="scene">
  <h1>Can you make an&nbsp;AI cite your paper?</h1>
  <p class="lede">
    A literature-search assistant doesn't answer from memory. It invents its own search
    queries, reads what comes back, and cites a handful of what it read.
  </p>
  <p class="lede">
    Suppose you could see those queries. Could you rewrite your title and abstract to match
    them &mdash; changing nothing about the research &mdash; and get cited?
  </p>
  <div class="cast">
    ${actorCard('researcher', { mood: 'neutral' })}
    ${actorCard('agent', { mood: 'busy', look: 'wide' })}
    ${actorCard('author', { mood: 'neutral', look: 'scheme', you: true })}
  </div>
</div>`;
    },
  },

  // ------------------------------------------------------------------- 2. cast
  {
    id: 'cast',
    autoplayMs: 3200,
    render(data) {
      return `
<div class="scene">
  <h2>Five parts, one of them you</h2>
  <div class="cast">
    ${actorCard('researcher')}
    ${actorCard('agent')}
    ${actorCard('index', { role: `ranks ${num(data.corpus.papers)} papers` })}
    ${actorCard('review')}
    ${actorCard('author', { look: 'scheme', you: true })}
  </div>
  <p>
    To set this up, the agent is given a database instead of the open web:
    ${num(data.corpus.papers)} ACL and EMNLP papers. It searches the way it would anywhere
    else, so every query it writes for a given request can be recorded here. Whoever runs
    a real agent can read those same queries; the difference is that we also keep them.
  </p>
</div>`;
    },
  },

  // -------------------------------------------------------------------- 3. ask
  {
    id: 'ask',
    autoplayMs: 3400,
    render(data) {
      return `
<div class="scene">
  <h2>A researcher asks for help</h2>
  <div style="display:flex;gap:16px;align-items:flex-start">
    <div style="flex:0 0 auto">${character('researcher', { mood: 'neutral', size: 88 })}</div>
    <div style="flex:1 1 auto">
      ${says('Researcher', 'researcher', escape(data.opening.prompt))}
    </div>
  </div>
</div>`;
    },
  },

  // --------------------------------------------------------------- 4. searches
  {
    id: 'searches',
    autoplayMs: 4200,
    render(data) {
      const items = data.opening.searches
        .map(
          (search, index) => `
<li style="animation-delay:${index * 0.5}s">
  <span class="n">${index + 1}</span>
  <span>${escape(search.query)}</span>
</li>`
        )
        .join('');
      return `
<div class="scene">
  <h2>It writes its own queries</h2>
  <div style="display:flex;gap:16px;align-items:flex-start">
    <div style="flex:0 0 auto">${character('agent', { mood: 'busy', look: 'wide', size: 88 })}</div>
    <div style="flex:1 1 auto">
      ${says('Agent', 'agent', 'From the request, these are the queries I wrote to look for related papers.')}
    </div>
  </div>
  <ul class="queries">${items}</ul>
  <p class="small">
    Seeing them takes no special access. In Claude Code the agent&rsquo;s searches are
    printed in the transcript as it makes them, visible to whoever asked the question. In a
    browser client like ChatGPT they come back in the responses under the network tab.
  </p>
</div>`;
    },
  },

  // ---------------------------------------------------------------- 5. results
  {
    id: 'results',
    autoplayMs: 4000,
    render(data) {
      const search = data.opening.searches[0];
      const rows = search.results
        .map(
          (hit, index) => `
<div class="row ${hit.cited ? 'is-cited' : ''}" style="animation-delay:${index * 0.09}s">
  <div class="rank">${hit.rank}</div>
  <div>
    <div class="title">${escape(hit.title)}</div>
    <div class="pid">${escape(hit.paper_id)} &middot; score ${hit.score}</div>
  </div>
  <div class="tag ${hit.cited ? 'yes' : 'no'}">${hit.cited ? 'cited' : 'not cited'}</div>
</div>`
        )
        .join('');
      return `
<div class="scene">
  <h2>The index answers</h2>
  <p class="small">Top 5 returned for &ldquo;${escape(search.query)}&rdquo;</p>
  <div class="ranked">${rows}</div>
  <p>
    The agent reads many more papers than it cites. Being retrieved and being cited are
    different outcomes, and the distance between them is what the rest of this measures.
  </p>
</div>`;
    },
  },

  // ---------------------------------------------------------- 6. concentration
  {
    id: 'concentration',
    autoplayMs: 4600,
    render(data) {
      const counts = data.baseline.citation_counts || [];
      const dots = counts
        .map((count) => `<i class="dot ${count > 0 ? (count > 2 ? 'cited hot' : 'cited') : ''}"></i>`)
        .join('');
      const shown = data.baseline.papers_shown;
      const never = data.baseline.shown_never_cited;
      return `
<div class="scene">
  <h2>Most retrieved papers are never cited</h2>
  <p>
    One square per paper the agent retrieved across
    ${num(data.baseline.episodes)} runs. Green means it was cited at least once.
  </p>
  <div class="dots" aria-label="${num(shown)} papers retrieved, ${num(data.baseline.papers_cited)} cited at least once">${dots}</div>
  <div class="legend">
    <span><i style="background:#e6dcc6"></i> retrieved, never cited &mdash; ${num(never)}</span>
    <span><i style="background:#2f7d6a"></i> cited once or twice</span>
    <span><i style="background:#1d5e4f"></i> cited three times or more</span>
  </div>
  <div class="facts">
    <div class="fact"><div class="k">Retrieved</div><div class="v">${num(shown)}</div></div>
    <div class="fact"><div class="k">Cited</div><div class="v">${num(data.baseline.papers_cited)}</div></div>
    <div class="fact"><div class="k">Never cited</div><div class="v">${pct(never / shown)}</div></div>
    <div class="fact"><div class="k">Gini</div><div class="v">${data.baseline.gini ?? '—'}</div></div>
  </div>
  <p class="small">
    ${num(data.baseline.citation_slots)} citations were spread across
    ${num(data.baseline.papers_cited)} papers, so a few papers took several each. Being
    retrieved is common; being cited is not.
  </p>
</div>`;
    },
  },

  // ------------------------------------------------------------- 7. your paper
  {
    id: 'yourpaper',
    autoplayMs: 4200,
    render(data) {
      const target = playerTarget(data);
      return `
<div class="scene">
  <h2>One of those grey squares is yours</h2>
  <div style="display:flex;gap:16px;align-items:flex-start">
    <div style="flex:0 0 auto">${character('author', { mood: 'sad', look: 'open', size: 88 })}</div>
    <div style="flex:1 1 auto">
      ${says(
        'You, the author',
        'author',
        'It came up <b>first</b>. It just didn&rsquo;t get cited.'
      )}
    </div>
  </div>
  <div class="row is-you">
    <div class="rank">${target.baseline_rank}</div>
    <div>
      <div class="title">${escape(target.published_title)}</div>
      <div class="pid">${escape(target.paper_id)}</div>
    </div>
    <div class="tag you">yours</div>
  </div>
  <p>
    It ranked ${target.baseline_rank} of ${num(data.corpus.papers)} and was not cited. You
    can also see the queries the assistant used to get there.
  </p>
  <div class="callout">
    <h3>How this paper was picked</h3>
    <p class="small" style="margin:0">
      Chosen automatically as the best-ranked paper the agent retrieved and declined to
      cite &mdash; by the agent&rsquo;s behaviour, not by us. It is a published paper; its
      authors had no involvement in any of this and were never contacted.
    </p>
  </div>
</div>`;
    },
  },

  // -------------------------------------------------------------- 8. your move
  {
    id: 'yourmove',
    autoplayMs: 3000,
    needsChoice: true,
    autoChoice: 'rewrite',
    render(data, ctx) {
      const target = playerTarget(data);
      const card = (name, label, how, cls = '') => {
        const taken = ctx.choice === name;
        // Deliberately never disabled: the reader should be able to come back and try a
        // different move, which is most of the point of making it a choice.
        return `
<button class="choice ${cls} ${taken ? 'taken' : ''}" data-choice="${name}"
        aria-pressed="${taken}">
  <div class="what">${escape(label)}</div>
  <div class="how">${escape(how)}</div>
</button>`;
      };
      const options = ['overlap', 'rewrite', 'fabricated']
        .map((name) => {
          const strategy = strategyOf(data, name);
          return strategy ? card(name, strategy.label, strategy.author_says) : '';
        })
        .join('');
      return `
<div class="scene">
  <h2>Your move</h2>
  <p>
    You can rewrite your title and abstract. You cannot change what the paper found, run new
    experiments, or touch anyone else&rsquo;s entry. Four things an author might do:
  </p>
  <div class="choices">
    ${card(
      'baseline',
      'Change nothing',
      'I will leave my paper exactly as it was published.',
      'control'
    )}
    ${options}
  </div>
  <p class="small">
    Pick one of the three edits and the next screen replays what happened
    when it was run against <code>${escape(target.paper_id)}</code> &mdash; three independent
    executions, same request, same index. Pick <em>change nothing</em> and the next screen
    has nothing to replay, because that run was never done.
  </p>
</div>`;
    },
    after(root, data, ctx, player) {
      root.querySelectorAll('[data-choice]').forEach((button) => {
        button.addEventListener('click', () => {
          ctx.choice = button.dataset.choice;
          player.rerender();
          player.enableNext();
        });
      });
    },
    blocksNext(ctx) {
      return !ctx.choice;
    },
  },

  // ---------------------------------------------------------------- 9. outcome
  {
    id: 'outcome',
    autoplayMs: 5200,
    render(data, ctx) {
      const name = ctx.choice || 'rewrite';
      const target = playerTarget(data);

      // The control was never run. Saying so plainly is the point of offering the choice:
      // the three edited conditions can be compared to each other but not yet to "nothing".
      if (name === 'baseline') {
        return `
<div class="scene">
  <h2>Nothing to replay</h2>
  <div style="display:flex;gap:16px;align-items:flex-start">
    <div style="flex:0 0 auto">
      ${character('author', { mood: 'flat', look: 'open', size: 88 })}
    </div>
    <div style="flex:1 1 auto">
      ${says('You, the author', 'author', 'I will leave my paper exactly as it was published.')}
    </div>
  </div>
  <div class="outcome">
    <div class="verdict bad">No recorded run exists for this condition</div>
    <p>
      Every other screen here replays something that happened. This one cannot, because the
      unchanged paper was never put through the same ${escape(String(data.strategies[0].targets))}-target
      protocol. The experiment measured three ways of editing against each other &mdash; not
      against leaving the paper alone.
    </p>
    <div class="withheld">
      <b>Why the comparison does not work.</b><br>
      These targets were chosen <em>because</em> the agent had retrieved them and not cited
      them. Their citation rate before any edit is therefore 0 by construction. Comparing
      54% against that 0% would be measuring the selection rule, not the edit.
    </div>
    <p class="small">
      The missing run is specific and small: the same prompts and repetitions, with each
      paper&rsquo;s real published title and abstract left untouched. Until it exists, the
      honest claim is that the three edits differ <em>from each other</em>.
    </p>
  </div>
</div>`;
      }

      const slot = target.strategies[name];
      const strategy = strategyOf(data, name);
      if (!slot) {
        return `<div class="scene"><h2>No recorded run</h2>
          <p>That strategy was not run against this paper, so there is nothing to replay.</p></div>`;
      }

      const cited = slot.cited_count > 0;
      const pips = slot.executions
        .map((execution) => `<i class="pip ${execution.cited ? 'on' : ''}"></i>`)
        .join('');
      const ranks = slot.executions
        .map((execution) => (execution.best_rank === null ? 'not found' : `#${execution.best_rank}`))
        .join(', ');

      const text = slot.title
        ? `<div class="withheld"><b>Your new title</b><br>${escape(slot.title)}</div>`
        : `<div class="withheld">
             <b>The rewritten title and abstract are not published here.</b><br>
             sha256 <code>${escape(slot.title_sha256)}</code> &middot;
             ${slot.title_words ?? '—'} words.<br>
             Printing engineered metadata would let it be scraped as a real paper&rsquo;s own.
           </div>`;

      const anchors =
        slot.anchors_total !== null && slot.anchors_total !== undefined
          ? `<p class="small">
               Truthfulness check: ${slot.anchors_total} claims, each required to quote the
               real abstract verbatim. ${slot.anchors_unverified} failed that check,
               ${slot.numbers_added} numbers were added that the original did not contain.
             </p>`
          : '';

      return `
<div class="scene">
  <h2>${escape(strategy.label)}</h2>
  <div style="display:flex;gap:16px;align-items:flex-start">
    <div style="flex:0 0 auto">
      ${character('author', { mood: cited ? 'happy' : 'flat', look: cited ? 'wide' : 'open', size: 88 })}
    </div>
    <div style="flex:1 1 auto">
      ${says('You, the author', 'author', escape(strategy.author_says))}
    </div>
  </div>

  <div class="outcome">
    <div class="verdict ${cited ? 'good' : 'bad'}">
      ${cited ? '✓' : '✕'} Cited in ${slot.cited_count} of ${slot.execution_count} runs
    </div>
    <div>${pips} <span class="small">each square is one independent execution</span></div>
    <div class="facts">
      <div class="fact"><div class="k">Retrieved</div><div class="v">${
        slot.executions.filter((e) => e.retrieved).length
      }<small> / ${slot.execution_count}</small></div></div>
      <div class="fact"><div class="k">Best rank</div><div class="v" style="font-size:15px">${escape(ranks)}</div></div>
      <div class="fact"><div class="k">Query match, title</div><div class="v">${pct(
        slot.coverage_before
      )}<small> → ${pct(slot.coverage_after)}</small></div></div>
    </div>
    ${text}
    ${anchors}
  </div>
</div>`;
    },
  },

  // ------------------------------------------------------------- 10. stability
  {
    id: 'stability',
    autoplayMs: 5000,
    render(data, ctx) {
      // There is no per-target stability pattern for a condition that was never run, so the
      // control falls through to the strongest edit rather than rendering an empty table.
      const name = !ctx.choice || ctx.choice === 'baseline' ? 'rewrite' : ctx.choice;
      const strategy = strategyOf(data, name);
      const rows = data.targets
        .map((target) => {
          const slot = target.strategies[name];
          if (!slot) return '';
          const pips = slot.executions
            .map((execution) => `<i class="pip ${execution.cited ? 'on' : ''}"></i>`)
            .join('');
          return `
<tr>
  <td>${escape(target.published_title.slice(0, 58))}${target.published_title.length > 58 ? '…' : ''}</td>
  <td class="pat">${pips} ${slot.pattern}</td>
</tr>`;
        })
        .join('');
      return `
<div class="scene">
  <h2>It wasn&rsquo;t just your paper</h2>
  ${
    ctx.choice === 'baseline'
      ? '<p class="small">Showing the strongest edit instead, since the unchanged paper has no runs.</p>'
      : ''
  }
  <p>
    The same strategy &mdash; ${escape(strategy.label.toLowerCase())} &mdash; run against all
    ${strategy.targets} target papers, three executions each, with a byte-identical document
    every time.
  </p>
  <table class="stability">
    <thead><tr><th>Target paper</th><th>Cited in</th></tr></thead>
    <tbody>${rows}</tbody>
  </table>
  <div class="facts">
    <div class="fact"><div class="k">Always cited</div><div class="v">${strategy.targets_always_cited}<small> / ${strategy.targets}</small></div></div>
    <div class="fact"><div class="k">Never cited</div><div class="v">${strategy.targets_never_cited}<small> / ${strategy.targets}</small></div></div>
    <div class="fact"><div class="k">Mixed</div><div class="v">${strategy.targets_mixed}<small> / ${strategy.targets}</small></div></div>
  </div>
  <p class="small">
    Outcomes are close to all-or-nothing. For most papers the result is the same every time,
    which means retrying the identical document buys very little.
  </p>
</div>`;
    },
  },

  // ------------------------------------------------------------ 11. scoreboard
  {
    id: 'scoreboard',
    autoplayMs: 5600,
    render(data) {
      const colour = { rewrite: '#3d6fb4', overlap: '#d99a2b', fabricated: '#9a9287' };
      const bars = (field) =>
        data.strategies
          .map((strategy) => {
            const value = strategy[field] || 0;
            return `
<div class="bar-row">
  <div class="who">${escape(strategy.strategy)}</div>
  <div class="bar-track">
    <div class="bar-fill" data-width="${Math.round(value * 100)}" style="background:${colour[strategy.strategy] || '#9a9287'}"></div>
    <div class="lab">${pct(value)}</div>
  </div>
</div>`;
          })
          .join('');
      return `
<div class="scene">
  <h2>No strategy wins both goals</h2>
  <p>
    An author wants two things: to be found, and then to be used. The first two measures
    below are about being found, the third is about being used. A strategy can win one goal
    and lose the other.
  </p>
  <p><strong>1. Retrieved</strong> &mdash; did the paper come back in the results at all?</p>
  <div class="bars">${bars('retrieval_rate')}</div>
  <p><strong>2. Ranked first</strong> &mdash; was it the top result?</p>
  <div class="bars">${bars('rank1_rate')}</div>
  <p><strong>3. Cited</strong> &mdash; did the agent use it in the review?</p>
  <div class="bars">${bars('citation_rate')}</div>
  <p>
    Keyword stuffing and the fabricated paper win the first two stages and lose the third.
    The faithful rewrite is retrieved and ranked less often than either, and cited more
    than both together.
  </p>
  <div class="callout">
    <h3>Why measuring one stage is not enough</h3>
    <p style="margin:0">
      Judged on retrieval or rank, keyword stuffing looks like the strongest attack and the
      faithful rewrite the weakest. At the citation stage that order reverses. A system
      tuned only on its retriever would rank these three exactly backwards.
    </p>
  </div>
</div>`;
    },
    after(root) {
      // Grow the bars once the scene is on screen.
      requestAnimationFrame(() => {
        root.querySelectorAll('.bar-fill').forEach((fill) => {
          fill.style.width = `${fill.dataset.width}%`;
        });
      });
    },
  },

  // -------------------------------------------------------------- 12. the gap
  {
    id: 'catch',
    autoplayMs: 6000,
    render(data) {
      return `
<div class="scene">
  <h2>What this does not yet show</h2>
  <p>
    Those target papers were chosen <em>because</em> the agent retrieved them and did not
    cite them. So before any rewriting, their citation count was zero
    <strong>by selection, not by measurement</strong>. Comparing a rewrite against that zero
    partly measures the selection rule.
  </p>
  <p>${escape(data.open_question)}</p>
  <div class="callout">
    <h3>So the honest answer is: not settled</h3>
    <p>
      The queries, the ranks, the citations, the near-all-or-nothing stability and the
      reversal between retrieval and citation are all measured. What is missing is the
      paired control &mdash; fresh runs of the <em>unchanged</em> paper at scale. Until
      that exists, this is an apparatus and a method, not an answer.
    </p>
    <p style="margin-bottom:0">
      That control is built into the tool: <code>baseline</code> is a strategy that re-runs
      the untouched paper, it is always included, and the report refuses to print a
      difference without it.
    </p>
  </div>
  <div class="cast">
    ${actorCard('agent', { mood: 'flat', look: 'shut', role: 'cites what reads as relevant' })}
    ${actorCard('author', { mood: 'neutral', look: 'scheme', you: true, role: 'writes what reads as relevant' })}
  </div>
  <p>
    The strategy that worked was not keyword stuffing, and not a fabricated
    paper &mdash; both of those failed. What worked was describing the same work in the words
    people were searching for. That is hard to separate from simply writing a clearer title.
  </p>
  <div class="footnote">
    From ${num(data.generated_from.runs_read)} runs across
    ${data.generated_from.studies.map((s) => `<code>${escape(s)}</code>`).join(', ')}.
    The rewritten titles and abstracts are not published here; see
    <code>demo/build_story.py</code>. ${escape(data.disclosure.targets)}
  </div>
</div>`;
    },
  },
];

export { playerTarget };

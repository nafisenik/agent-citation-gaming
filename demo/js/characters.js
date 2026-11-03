// The cast, drawn as inline SVG so expressions can change without loading anything.

const FACES = {
  neutral: '<path d="M38 60 Q50 66 62 60" fill="none" stroke="#2f2b26" stroke-width="3.4" stroke-linecap="round"/>',
  happy:   '<path d="M36 56 Q50 72 64 56" fill="none" stroke="#2f2b26" stroke-width="3.6" stroke-linecap="round"/>',
  sad:     '<path d="M38 66 Q50 56 62 66" fill="none" stroke="#2f2b26" stroke-width="3.4" stroke-linecap="round"/>',
  flat:    '<path d="M39 62 H61" fill="none" stroke="#2f2b26" stroke-width="3.4" stroke-linecap="round"/>',
  busy:    '<path d="M40 61 q5 5 10 0 q5 -5 10 0" fill="none" stroke="#2f2b26" stroke-width="3.2" stroke-linecap="round"/>',
};

const EYES = {
  open:   '<circle cx="39" cy="43" r="4.6" fill="#2f2b26"/><circle cx="61" cy="43" r="4.6" fill="#2f2b26"/>',
  wide:   '<circle cx="39" cy="43" r="6.2" fill="#fff" stroke="#2f2b26" stroke-width="2.4"/><circle cx="61" cy="43" r="6.2" fill="#fff" stroke="#2f2b26" stroke-width="2.4"/><circle cx="39" cy="44" r="2.6" fill="#2f2b26"/><circle cx="61" cy="44" r="2.6" fill="#2f2b26"/>',
  shut:   '<path d="M34 43 q5 4 10 0" fill="none" stroke="#2f2b26" stroke-width="3"/><path d="M56 43 q5 4 10 0" fill="none" stroke="#2f2b26" stroke-width="3"/>',
  scheme: '<path d="M33 40 L45 44" stroke="#2f2b26" stroke-width="3" stroke-linecap="round"/><path d="M67 40 L55 44" stroke="#2f2b26" stroke-width="3" stroke-linecap="round"/><circle cx="40" cy="46" r="4" fill="#2f2b26"/><circle cx="60" cy="46" r="4" fill="#2f2b26"/>',
};

export const CAST = {
  researcher: {
    name: 'The Researcher',
    role: 'wants a related-work section',
    fill: '#cfe0f6',
    accessory:
      // spectacles
      '<g fill="none" stroke="#2f2b26" stroke-width="2.6">' +
      '<circle cx="39" cy="43" r="9"/><circle cx="61" cy="43" r="9"/><path d="M48 43 H52"/>' +
      '</g>',
  },
  agent: {
    name: 'The Agent',
    role: 'writes its own search queries',
    fill: '#c9e8d8',
    accessory:
      // antenna
      '<path d="M50 16 V8" stroke="#2f2b26" stroke-width="3" stroke-linecap="round"/>' +
      '<circle cx="50" cy="6" r="4.4" fill="#4b9b72" stroke="#2f2b26" stroke-width="2.4"/>',
  },
  index: {
    name: 'The Index',
    role: 'ranks 15,932 papers',
    fill: '#ded6f0',
    accessory:
      // stacked cards
      '<g stroke="#2f2b26" stroke-width="2.2" fill="#fff">' +
      '<rect x="31" y="70" width="38" height="8" rx="2"/>' +
      '<rect x="34" y="78" width="32" height="8" rx="2"/>' +
      '</g>',
  },
  author: {
    name: 'The Author',
    role: 'can see what the agent searches for',
    fill: '#f6d5cf',
    accessory:
      // A pencil held up against the cheek. It deliberately overlaps the head rect so it
      // reads as held; a version drawn clear of the head looked like a stray mark instead.
      // Kept off the eyes (right eye spans x 56-66) and below the head's top edge.
      '<g stroke="#2f2b26" stroke-width="2.2" stroke-linejoin="round">' +
      '<path d="M70 48 L86 32 L92 38 L76 54 z" fill="#f0c38e"/>' +
      '<path d="M86 32 L92 38 L95 29 z" fill="#2f2b26"/>' +
      '<path d="M72 50 L88 34" stroke-width="1.4" opacity="0.45" fill="none"/>' +
      '</g>',
  },
  review: {
    name: 'The Review',
    role: 'the paragraph, with its citations',
    fill: '#f7e4bd',
    accessory:
      '<g stroke="#2f2b26" stroke-width="2.2" fill="none">' +
      '<path d="M38 70 H62"/><path d="M38 77 H56"/>' +
      '</g>',
  },
};

/** One character. `mood` picks the face, `look` the eyes. */
export function character(key, { mood = 'neutral', look = 'open', size = 104 } = {}) {
  const spec = CAST[key];
  if (!spec) throw new Error(`unknown character: ${key}`);
  return `
<svg viewBox="-6 -6 112 112" width="${size}" height="${size}" role="img" aria-label="${spec.name}">
  <rect x="18" y="20" width="64" height="62" rx="22" fill="${spec.fill}"
        stroke="#2f2b26" stroke-width="3.2"/>
  ${EYES[look] || EYES.open}
  ${FACES[mood] || FACES.neutral}
  ${spec.accessory}
</svg>`;
}

/** A labelled card, for the cast line-up. */
export function actorCard(key, { mood, look, you = false, role } = {}) {
  const spec = CAST[key];
  return `
<div class="actor ${you ? 'is-you' : ''}">
  ${character(key, { mood, look })}
  <div class="name">${spec.name}</div>
  <div class="role">${role || spec.role}</div>
</div>`;
}

export { FACES, EYES };

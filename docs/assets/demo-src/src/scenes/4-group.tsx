import {Img, Rect, Txt, makeScene2D} from '@motion-canvas/2d';
import {all, createRef, easeOutBack, fadeTransition, waitFor} from '@motion-canvas/core';
import {C, H, SANS, W, caption, progress, shotCard} from '../lib';
import inbox1 from '../../shots/inbox-1.png';
import inbox2 from '../../shots/inbox-2.png';
import inbox3 from '../../shots/inbox-3.png';
import inbox4 from '../../shots/inbox-4.png';

// Scene 3: the same question keeps arriving; the real inbox shows one group whose count climbs 1 to 4.
const CARD_Y = 140;
const crop = {x: 140, y: 800, w: 1760, h: 430};
const CARD_W = 1160;
export default makeScene2D(function* (view) {
  view.fill(C.paper);
  yield* fadeTransition(0.45);
  const cap = createRef<Txt>();
  const card = createRef<Rect>();
  const pill = createRef<Rect>();
  const n = createRef<Txt>();
  const shots = [inbox2, inbox3, inbox4];
  const imgs = shots.map(() => createRef<Img>());
  const s = CARD_W / crop.w;

  view.add(
    <>
      {caption(cap, 'Repeats join the same entry. The count goes up, with no second alert.', C.dark)}
      {shotCard(card, inbox1, 2560, 1440, crop, CARD_W)}
      <Rect layout ref={pill} x={330} y={CARD_Y - 190} fill={C.green} radius={40} padding={[10, 30]} opacity={0} scale={0.6}>
        <Txt ref={n} text="1 occurrence" fontFamily={SANS} fontWeight={800} fontSize={46} letterSpacing={-1} fill="#fff" />
      </Rect>
      {/* Dim the other groups so the eye stays on the VPN row (first row is 73 to 158 px into the crop). */}
      <Rect y={CARD_Y + (crop.h * s) / 2 - (crop.h - 158) * s / 2} width={CARD_W} height={(crop.h - 158) * s} fill="rgba(246,248,246,0.72)" />
      {progress(4, false)}
    </>,
  );
  card().y(CARD_Y);
  // Later screenshots are layered inside the card and faded in over the first one.
  shots.forEach((src, i) => {
    card().add(
      <Img
        ref={imgs[i]}
        src={src}
        width={2560 * s}
        height={1440 * s}
        x={(2560 / 2 - (crop.x + crop.w / 2)) * s}
        y={(1440 / 2 - (crop.y + crop.h / 2)) * s}
        opacity={0}
      />,
    );
  });

  yield* waitFor(0.4);
  yield* all(pill().opacity(1, 0.3), pill().scale(1, 0.4, easeOutBack));
  yield* waitFor(0.8);
  for (let i = 0; i < shots.length; i++) {
    n().text(`${i + 2} occurrences`);
    yield* all(imgs[i]().opacity(1, 0.3), pill().scale(1.12, 0.15).back(0.2));
    yield* waitFor(i === shots.length - 1 ? 1.8 : 0.8);
  }
});

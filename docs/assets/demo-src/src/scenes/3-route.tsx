import {Rect, Txt, makeScene2D} from '@motion-canvas/2d';
import {all, createRef, easeOutCubic, fadeTransition, waitFor} from '@motion-canvas/core';
import {C, H, SANS, W, caption, progress, shotCard} from '../lib';
import routing from '../../shots/routing.png';
import deliveries from '../../shots/deliveries.png';

// Scene 4: a routing rule sends the group to the docs owner; the delivery log shows it delivered.
const RULE_Y = 85;
const LOG_Y = 245;
const ruleCrop = {x: 44, y: 160, w: 1330, h: 165};
const logCrop = {x: 140, y: 636, w: 1330, h: 160};
const logScale = 1120 / logCrop.w;

export default makeScene2D(function* (view) {
  view.fill(C.paper);
  yield* fadeTransition(0.45);
  const cap = createRef<Txt>();
  const rule = createRef<Rect>();
  const log = createRef<Rect>();
  const ring = createRef<Rect>();
  
  view.add(
    <>
      {caption(cap, 'A routing rule sends it to the docs owner, by webhook or email.', C.dark)}
      {shotCard(rule, routing, 1556, 982, ruleCrop, 1120)}
      {shotCard(log, deliveries, 2560, 1440, logCrop, 1120)}
      {/* First delivery row sits 56 to 144 px into the crop. */}
      <Rect ref={ring} y={LOG_Y - (logCrop.h * logScale) / 2 + 100 * logScale} width={1140} height={84} radius={10} stroke={C.green} lineWidth={5} opacity={0} />
      {progress(3, false)}
    </>,
  );
  rule().position([0, RULE_Y]).opacity(0);
  log().position([0, LOG_Y + 40]).opacity(0);

  yield* all(rule().opacity(1, 0.4), rule().y(RULE_Y - 10, 0.6, easeOutCubic));
  yield* waitFor(1.4);
  yield* all(log().opacity(1, 0.4), log().y(LOG_Y, 0.5, easeOutCubic));
  yield* waitFor(0.3);
  yield* ring().opacity(1, 0.3);
  yield* waitFor(1.6);
});

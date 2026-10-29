import {Layout, Rect, Txt, makeScene2D} from '@motion-canvas/2d';
import {all, createRef, waitFor} from '@motion-canvas/core';
import {C, H, MONO, SANS, W, caption, progress} from '../lib';

// Scene 1: the problem. A generic chat (no brand), then the point: nobody on the docs team hears about it.
export default makeScene2D(function* (view) {
  view.fill(C.ink);
  const cap = createRef<Txt>();
  const cap2 = createRef<Txt>();
  const user = createRef<Rect>();
  const agent = createRef<Rect>();
  const label = createRef<Txt>();

  view.add(
    <>
      {caption(cap, 'Your AI gets a question your docs don’t cover.', '#fff')}
      <Txt
        ref={cap2}
        text="The API docs team needs to hear about it."
        x={-W / 2 + 80}
        y={-H / 2 + 70}
        offset={[-1, -1]}
        width={W - 160}
        fontFamily={SANS}
        fontWeight={800}
        fontSize={54}
        lineHeight={60}
        letterSpacing={-1.5}
        textWrap
        fill={C.amber}
        opacity={0}
      />
      <Txt ref={label} text="YOUR AI ASSISTANT" x={-W / 2 + 80} y={10} offset={[-1, 0]} fontFamily={SANS} fontWeight={700} fontSize={20} letterSpacing={3} fill="#7fa493" />
      <Rect layout ref={user} x={W / 2 - 80} y={90} offset={[1, 0]} padding={[22, 32]} radius={[28, 28, 6, 28]} fill="#2f4a3f">
        <Txt text="How do I paginate the /orders endpoint?" fontFamily={SANS} fontWeight={700} fontSize={34} fill="#fff" width={660} textWrap />
      </Rect>
      <Rect layout ref={agent} x={-W / 2 + 80} y={210} offset={[-1, 0]} padding={[22, 32]} radius={[28, 28, 28, 6]} fill="#fff" opacity={0} scale={0.9} stroke={C.amber} lineWidth={0}>
        <Txt text="I don’t know. I couldn’t find that in my sources." fontFamily={SANS} fontWeight={700} fontSize={34} fill={C.ink} textWrap width={640} />
      </Rect>
      {progress(1, true)}
    </>,
  );

  yield* waitFor(0.7);
  yield* all(agent().opacity(1, 0.35), agent().scale(1, 0.35));
  yield* waitFor(0.9);
  yield* all(cap().opacity(0, 0.35), cap2().opacity(1, 0.35), agent().lineWidth(5, 0.4));
  yield* waitFor(1.5);
});

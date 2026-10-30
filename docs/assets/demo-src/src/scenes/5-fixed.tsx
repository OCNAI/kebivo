import {Layout, Rect, Txt, makeScene2D} from '@motion-canvas/2d';
import {all, createRef, fadeTransition, waitFor} from '@motion-canvas/core';
import {C, H, MONO, SANS, W, mark} from '../lib';

// End card: the documented outcome, without implying a documentation fix happened automatically.
export default makeScene2D(function* (view) {
  view.fill(C.paper);
  yield* fadeTransition(0.45);
  const end = createRef<Layout>();
  const fade = createRef<Rect>();
  view.add(<>
    <Layout ref={end} layout direction="column" alignItems="center" gap={34} opacity={0}>
      {mark(512)}
      <Txt text="See unanswered questions. Prioritize the repeats."
        fontFamily={SANS} fontWeight={700} fontSize={46} lineHeight={54}
        letterSpacing={-1} fill={C.dark} textAlign="center" width={980} textWrap />
      <Rect layout fill={C.ink} radius={12} padding={[16, 30]}>
        <Txt text="kebivo.com" fontFamily={MONO} fontWeight={700} fontSize={36} fill={C.mint} />
      </Rect>
      <Txt text="Open source · Self-hosted · One container" fontFamily={SANS} fontWeight={700} fontSize={22} fill={C.muted} />
    </Layout>
    <Rect ref={fade} width={W} height={H} fill={C.ink} opacity={0} />
  </>);
  yield* end().opacity(1, 0.4);
  yield* waitFor(2.2);
  yield* fade().opacity(1, 0.5);
});

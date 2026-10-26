import {Img, Layout, Rect, Txt} from '@motion-canvas/2d';
import logo from './logo.svg';
import {Reference, createRef} from '@motion-canvas/core';

export const C = {
  ink: '#14201b',
  dark: '#263d35',
  green: '#35735e',
  mint: '#bfe3d3',
  paper: '#f6f8f6',
  line: '#dce5de',
  muted: '#476455',
  amber: '#f0b45a',
};
export const SANS = 'Inter';
export const MONO = '"JetBrains Mono"';
export const W = 1280;
export const H = 720;

/** Large caption in the top-left of every scene. */
export function caption(ref: Reference<Txt>, text: string, fill: string, size = 54) {
  return (
    <Txt
      ref={ref}
      text={text}
      x={-W / 2 + 80}
      y={-H / 2 + 70}
      offset={[-1, -1]}
      width={W - 160}
      fontFamily={SANS}
      fontWeight={800}
      fontSize={size}
      lineHeight={size * 1.12}
      letterSpacing={-1.5}
      textWrap
      fill={fill}
    />
  );
}

/** Five progress ticks along the bottom; `active` is 1-based. */
export function progress(active: number, dark: boolean) {
  return (
    <Layout layout y={H / 2 - 34} gap={10}>
      {[1, 2, 3, 4, 5].map(i => (
        <Rect
          width={i === active ? 44 : 14}
          height={6}
          radius={3}
          fill={i === active ? (dark ? C.amber : C.green) : dark ? '#3a4d45' : C.line}
        />
      ))}
    </Layout>
  );
}

export interface Crop {
  x: number;
  y: number;
  w: number;
  h: number;
}

/** A framed card showing a cropped region of a real screenshot. `src` pixel size is (sw, sh). */
export function shotCard(ref: Reference<Rect>, src: string, sw: number, sh: number, crop: Crop, width: number) {
  const s = width / crop.w;
  const height = crop.h * s;
  return (
    <Rect
      ref={ref}
      width={width}
      height={height}
      radius={14}
      clip
      fill="#fff"
      stroke={C.line}
      lineWidth={2}
      shadowColor="rgba(20,32,27,0.22)"
      shadowBlur={36}
      shadowOffsetY={14}
    >
      <Img src={src} width={sw * s} height={sh * s} x={(sw / 2 - (crop.x + crop.w / 2)) * s} y={(sh / 2 - (crop.y + crop.h / 2)) * s} />
    </Rect>
  );
}

export function mark(size: number) {
  return <Img src={logo} width={size} height={size} />;
}

export {createRef};

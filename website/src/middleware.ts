import { defineMiddleware } from 'astro:middleware';
import sample from '../public/dayframe.json';
export const onRequest = defineMiddleware((context, next) => {
  if (import.meta.env.DEV && !context.locals.snapshot) context.locals.snapshot = structuredClone(sample);
  return next();
});

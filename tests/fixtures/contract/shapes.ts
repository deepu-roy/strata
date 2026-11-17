import { helper } from "./util";
import * as path from "path";

export interface Greeter {
  greet(name: string): string;
}

export class BaseGreeter {
  prefix(): string {
    return "Hello";
  }
}

export class PoliteGreeter extends BaseGreeter implements Greeter {
  greet(name: string): string {
    return this.prefix() + ", " + helper(name);
  }
}

export function run(): string {
  const g = new PoliteGreeter();
  return g.greet("world");
}

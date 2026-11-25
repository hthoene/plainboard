/** The icons an area of a doc site's navigation may have (#159; the same list as services/docs_areas.ICONS). */
import {
  BookOpen, Box, Cloud, Code, Compass, Database, FileText, Globe, GraduationCap, Layers, LifeBuoy, Lightbulb, Map as MapIcon, Newspaper, Plug, Puzzle, Rocket, Server,
  Settings, Shield, Terminal, Users, Wrench, Zap, type LucideIcon,
} from "lucide-react";

export const AREA_ICONS: Record<string, LucideIcon> = {
  "book-open": BookOpen, rocket: Rocket, code: Code, terminal: Terminal, settings: Settings, shield: Shield, server: Server, cloud: Cloud,
  database: Database, plug: Plug, puzzle: Puzzle, layers: Layers, compass: Compass, "graduation-cap": GraduationCap, lightbulb: Lightbulb,
  wrench: Wrench, users: Users, newspaper: Newspaper, "life-buoy": LifeBuoy, map: MapIcon, box: Box, zap: Zap, globe: Globe, "file-text": FileText,
};

export const areaIcon = (name: string): LucideIcon => AREA_ICONS[name] ?? BookOpen;

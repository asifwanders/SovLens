export type MediaItem = {
  id: string;
  type: "image" | "video" | "audio_segment";
  src: string;
  thumbnail?: string;
  path: string;
  timestamp?: number;
  is_primary?: boolean;
  video_id?: string;
  score?: number;
  text_snippet?: string;
};

export interface LevelInfo {
  key: string;
  label: string;
  description: string;
  speed_estimate: string;
}

export interface ConfigResponse {
  level: string;
  level_data: Record<string, unknown>;
  available_levels: LevelInfo[];
}

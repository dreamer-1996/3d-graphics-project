#version 330 core

uniform sampler2D diffuse_map;
in vec2 frag_tex_coords;
in vec2 frag_uv;

out vec4 out_color;

void main() {
    out_color = texture(diffuse_map, frag_uv);
}

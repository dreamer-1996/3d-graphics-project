# Python built-in modules
import os  # os function, i.e. checking file status
import sys  # for sys.exit
from bisect import bisect_left
from itertools import cycle  # allows easy circular choice list

# External, non built-in modules
import OpenGL.GL as GL  # standard Python OpenGL wrapper
import assimpcy
import glfw  # lean window system wrapper for OpenGL
import numpy as np  # all matrix manipulations & OpenGL args

# our transform functions
from PIL import Image

from transform import Trackball, identity, translate, lookat, perspective, quaternion_matrix, scale, quaternion_slerp, \
    lerp
from camera import Camera


# from keyframe import TransformKeyFrames, KeyFrameControlNode
# from skinning import SkinningControlNode, MAX_BONES, MAX_VERTEX_BONES


def load_textured(file, shader, tex_file=None):
    """ load resources from file using assimp, return list of TexturedMesh """
    try:
        pp = assimpcy.aiPostProcessSteps
        flags = pp.aiProcess_Triangulate | pp.aiProcess_FlipUVs
        scene = assimpcy.aiImportFile(file, flags)
    except assimpcy.all.AssimpError as exception:
        print('ERROR loading', file + ': ', exception.args[0].decode())
        return []

    # Note: embedded textures not supported at the moment
    path = os.path.dirname(file) if os.path.dirname(file) != '' else './'
    for mat in scene.mMaterials:
        if not tex_file and 'TEXTURE_BASE' in mat.properties:  # texture token
            name = os.path.basename(mat.properties['TEXTURE_BASE'])
            # search texture in file's whole subdir since path often screwed up
            paths = os.walk(path, followlinks=True)
            found = [os.path.join(d, f) for d, _, n in paths for f in n
                     if name.startswith(f) or f.startswith(name)]
            assert found, 'Cannot find texture %s in %s subtree' % (name, path)
            tex_file = found[0]
        if tex_file:
            mat.properties['diffuse_map'] = Texture(tex_file=tex_file)

    # prepare textured mesh
    meshes = []
    for mesh in scene.mMeshes:
        mat = scene.mMaterials[mesh.mMaterialIndex].properties
        assert mat['diffuse_map'], "Trying to map using a textureless material"
        attributes = [mesh.mVertices, mesh.mTextureCoords[0], mesh.mNormals]
        mesh = TexturedMesh(shader, mat['diffuse_map'], attributes, mesh.mFaces)
        meshes.append(mesh)

    size = sum((mesh.mNumFaces for mesh in scene.mMeshes))
    print('Loaded %s\t(%d meshes, %d faces)' % (file, len(meshes), size))
    return meshes


def multi_load_textured(file, shader, tex_file, k_a, k_d, k_s, s):
    """ load resources from file using assimp, return list of TexturedMesh """
    try:
        pp = assimpcy.aiPostProcessSteps
        flags = pp.aiProcess_Triangulate | pp.aiProcess_FlipUVs
        scene = assimpcy.aiImportFile(file, flags)
    except assimpcy.all.AssimpError as exception:
        print('ERROR loading', file + ': ', exception.args[0].decode())
        return []
    print("materials: ", scene.mNumMaterials)
    # Note: embedded textures not supported at the moment
    path = os.path.dirname(file) if os.path.dirname(file) != '' else './'
    for index, mat in enumerate(scene.mMaterials):
        if not tex_file and 'TEXTURE_BASE' in mat.properties:  # texture token
            name = os.path.basename(mat.properties['TEXTURE_BASE'])
            # search texture in file's whole subdir since path often screwed up
            paths = os.walk(path, followlinks=True)
            found = [os.path.join(d, f) for d, _, n in paths for f in n
                     if name.startswith(f) or f.startswith(name)]
            assert found, 'Cannot find texture %s in %s subtree' % (name, path)
            tex_file = found[0]
        if tex_file:
            print("Index: ", index)
            mat.properties['diffuse_map'] = Texture(tex_file=tex_file[index])

    # prepare textured mesh
    meshes = []
    for mesh in scene.mMeshes:
        mat = scene.mMaterials[mesh.mMaterialIndex].properties
        assert mat['diffuse_map'], "Trying to map using a textureless material"
        attributes = [mesh.mVertices, mesh.mTextureCoords[0], mesh.mNormals]
        mesh = TexturedPhongMesh(shader, mat['diffuse_map'], attributes, mesh.mFaces,
                                 k_d=k_d, k_a=k_a, k_s=k_s, s=s)
        meshes.append(mesh)

    size = sum((mesh.mNumFaces for mesh in scene.mMeshes))
    print('Loaded %s\t(%d meshes, %d faces)' % (file, len(meshes), size))
    return meshes


# ------------ low level OpenGL object wrappers ----------------------------
class Shader:
    """ Helper class to create and automatically destroy shader program """

    @staticmethod
    def _compile_shader(src, shader_type):
        src = open(src, 'r').read() if os.path.exists(src) else src
        src = src.decode('ascii') if isinstance(src, bytes) else src
        shader = GL.glCreateShader(shader_type)
        GL.glShaderSource(shader, src)
        GL.glCompileShader(shader)
        status = GL.glGetShaderiv(shader, GL.GL_COMPILE_STATUS)
        src = ('%3d: %s' % (i + 1, l) for i, l in enumerate(src.splitlines()))
        if not status:
            log = GL.glGetShaderInfoLog(shader).decode('ascii')
            GL.glDeleteShader(shader)
            src = '\n'.join(src)
            print('Compile failed for %s\n%s\n%s' % (shader_type, log, src))
            sys.exit(1)
        return shader

    def __init__(self, vertex_source, fragment_source):
        """ Shader can be initialized with raw strings or source file names """
        self.glid = None
        vert = self._compile_shader(vertex_source, GL.GL_VERTEX_SHADER)
        frag = self._compile_shader(fragment_source, GL.GL_FRAGMENT_SHADER)
        if vert and frag:
            self.glid = GL.glCreateProgram()  # pylint: disable=E1111
            GL.glAttachShader(self.glid, vert)
            GL.glAttachShader(self.glid, frag)
            GL.glLinkProgram(self.glid)
            GL.glDeleteShader(vert)
            GL.glDeleteShader(frag)
            status = GL.glGetProgramiv(self.glid, GL.GL_LINK_STATUS)
            if not status:
                print(GL.glGetProgramInfoLog(self.glid).decode('ascii'))
                sys.exit(1)

    # def __del__(self):
    #     GL.glUseProgram(0)
    #     if self.glid:  # if this is a valid shader object
    #         GL.glDeleteProgram(self.glid)  # object dies => destroy GL object


class VertexArray:
    """ helper class to create and self destroy OpenGL vertex array objects."""

    def __init__(self, attributes, index=None, usage=GL.GL_STATIC_DRAW):
        """ Vertex array from attributes and optional index array. Vertex
            Attributes should be list of arrays with one row per vertex. """

        # create vertex array object, bind it
        self.glid = GL.glGenVertexArrays(1)
        GL.glBindVertexArray(self.glid)
        self.buffers = []  # we will store buffers in a list
        nb_primitives, size = 0, 0

        # load buffer per vertex attribute (in list with index = shader layout)
        for loc, data in enumerate(attributes):
            if data is not None:
                # bind a new vbo, upload its data to GPU, declare size and type
                self.buffers.append(GL.glGenBuffers(1))
                data = np.array(data, np.float32, copy=False)  # ensure format
                # print(data.shape)
                nb_primitives, size = data.shape
                # print("nb_primitives:", nb_primitives)
                # print("size:", size)
                GL.glEnableVertexAttribArray(loc)
                GL.glBindBuffer(GL.GL_ARRAY_BUFFER, self.buffers[-1])
                GL.glBufferData(GL.GL_ARRAY_BUFFER, data, usage)
                GL.glVertexAttribPointer(loc, size, GL.GL_FLOAT, False, 0, None)

        # optionally create and upload an index buffer for this object
        self.draw_command = GL.glDrawArrays
        self.arguments = (0, nb_primitives)
        if index is not None:
            self.buffers += [GL.glGenBuffers(1)]
            index_buffer = np.array(index, np.int32, copy=False)  # good format
            GL.glBindBuffer(GL.GL_ELEMENT_ARRAY_BUFFER, self.buffers[-1])
            GL.glBufferData(GL.GL_ELEMENT_ARRAY_BUFFER, index_buffer, usage)
            self.draw_command = GL.glDrawElements
            self.arguments = (index_buffer.size, GL.GL_UNSIGNED_INT, None)
        # GL.glBindVertexArray(0)

    def execute(self, primitive):
        """ draw a vertex array, either as direct array or indexed array """
        GL.glBindVertexArray(self.glid)
        self.draw_command(primitive, *self.arguments)

    # def __del__(self):  # object dies => kill GL array and buffers from GPU
    #     GL.glDeleteVertexArrays(1, [self.glid])
    #     GL.glDeleteBuffers(len(self.buffers), self.buffers)


# ------------  Mesh is a core drawable, can be basis for most objects --------
class Mesh:
    """ Basic mesh class with attributes passed as constructor arguments """

    def __init__(self, shader, attributes, index=None):
        self.shader = shader
        self.names = ['view', 'projection', 'model']
        self.loc = {n: GL.glGetUniformLocation(shader.glid, n) for n in self.names}
        self.vertex_array = VertexArray(attributes, index)

    def draw(self, projection, view, model, primitives=GL.GL_TRIANGLES):
        GL.glUseProgram(self.shader.glid)

        GL.glUniformMatrix4fv(self.loc['view'], 1, True, view)
        GL.glUniformMatrix4fv(self.loc['projection'], 1, True, projection)
        GL.glUniformMatrix4fv(self.loc['model'], 1, True, model)

        # draw triangle as GL_TRIANGLE vertex array, draw array call
        self.vertex_array.execute(primitives)


# ------------  Node is the core drawable for hierarchical scene graphs -------
class Node:
    """ Scene graph transform and parameter broadcast node """

    def __init__(self, children=(), transform=identity()):
        self.transform = transform
        self.children = list(iter(children))

    def add(self, *drawables):
        """ Add drawables to this node, simply updating children list """
        self.children.extend(drawables)

    def draw(self, projection, view, model):
        """ Recursive draw, passing down updated model matrix. """
        for child in self.children:
            child.draw(projection, view, model @ self.transform)  # TODO TP3: hierarchical update

    def key_handler(self, key):
        """ Dispatch keyboard events to children """
        for child in self.children:
            if hasattr(child, 'key_handler'):
                child.key_handler(key)


# ------------  Viewer class & window management ------------------------------
class Viewer(Node):
    """ GLFW viewer window, with classic initialization & graphics loop """

    def __init__(self, width=640, height=480):
        super().__init__()

        self.width = width
        self.height = height
        self.camera = Camera()
        self.last_frame = 0.0

        # version hints: create GL window with >= OpenGL 3.3 and core profile
        glfw.window_hint(glfw.CONTEXT_VERSION_MAJOR, 3)
        glfw.window_hint(glfw.CONTEXT_VERSION_MINOR, 3)
        glfw.window_hint(glfw.OPENGL_FORWARD_COMPAT, GL.GL_TRUE)
        glfw.window_hint(glfw.OPENGL_PROFILE, glfw.OPENGL_CORE_PROFILE)
        glfw.window_hint(glfw.RESIZABLE, True)
        self.win = glfw.create_window(width, height, 'Viewer', None, None)

        # make win's OpenGL context current; no OpenGL calls can happen before
        glfw.make_context_current(self.win)

        # initialize trackball
        self.trackball = Trackball()
        self.mouse = (0, 0)

        # glfw.set_cursor_pos_callback(window=self.win, cbfun=self.camera.process_mouse_movement)

        # register event handlers
        glfw.set_key_callback(self.win, self.on_key)
        # glfw.set_cursor_pos_callback(self.win, self.on_mouse_move)
        # glfw.set_scroll_callback(self.win, self.on_scroll)
        glfw.set_window_size_callback(self.win, self.on_size)

        # useful message to check OpenGL renderer characteristics
        print('OpenGL', GL.glGetString(GL.GL_VERSION).decode() + ', GLSL',
              GL.glGetString(GL.GL_SHADING_LANGUAGE_VERSION).decode() +
              ', Renderer', GL.glGetString(GL.GL_RENDERER).decode())

        # initialize GL by setting viewport and default render characteristics
        GL.glClearColor(0.1, 0.1, 0.1, 0.1)
        GL.glEnable(GL.GL_CULL_FACE)  # backface culling enabled (TP2)
        GL.glEnable(GL.GL_DEPTH_TEST)  # depth test now enabled (TP2)

        # cyclic iterator to easily toggle polygon rendering modes
        self.fill_modes = cycle([GL.GL_LINE, GL.GL_POINT, GL.GL_FILL])

        self.model = identity()

    def run(self):
        """ Main render loop for this OpenGL window """
        while not glfw.window_should_close(self.win):
            # clear draw buffer and depth buffer (<-TP2)
            GL.glClear(GL.GL_COLOR_BUFFER_BIT | GL.GL_DEPTH_BUFFER_BIT)

            # win_size = glfw.get_window_size(self.win)
            # view = self.trackball.view_matrix()
            # projection = self.trackball.projection_matrix(win_size)

            # Calculate the time between last rendered frame
            current_frame = glfw.get_time()
            delta_time = current_frame - self.last_frame
            self.last_frame = current_frame

            # Update the view matrix with camera orientation
            view = lookat(eye=self.camera.get_camera_pos(),
                          target=self.camera.get_camera_pos() + self.camera.get_camera_front(),
                          up=self.camera.get_camera_up())

            # Update the projection matrix
            projection = perspective(fovy=self.camera.get_fov(), aspect=(self.width / self.height), near=0.1, far=500.0)

            # draw our scene objects
            self.draw(projection, view, identity())

            # flush render commands, and swap draw buffers
            glfw.swap_buffers(self.win)

            # Poll for and process events
            glfw.poll_events()

            # Get ASDF inputs from user for camera POV
            self.camera.process_keyboard_input(window=self.win, delta_time=delta_time)

    def on_key(self, _win, key, _scancode, action, _mods):
        """ 'Q' or 'Escape' quits """
        if action == glfw.PRESS or action == glfw.REPEAT:
            if key == glfw.KEY_ESCAPE or key == glfw.KEY_Q:
                glfw.set_window_should_close(self.win, True)
            if key == glfw.KEY_R:
                GL.glPolygonMode(GL.GL_FRONT_AND_BACK, next(self.fill_modes))
            if key == glfw.KEY_SPACE:
                glfw.set_time(0)

            # call Node.key_handler which calls key_handlers for all drawables
            self.key_handler(key)

    def on_mouse_move(self, win, xpos, ypos):
        """ Rotate on left-click & drag, pan on right-click & drag """
        old = self.mouse
        self.mouse = (xpos, glfw.get_window_size(win)[1] - ypos)
        if glfw.get_mouse_button(win, glfw.MOUSE_BUTTON_LEFT):
            self.trackball.drag(self.mouse, old, glfw.get_window_size(win))
        if glfw.get_mouse_button(win, glfw.MOUSE_BUTTON_RIGHT):
            self.trackball.pan(old, self.mouse)

    def on_scroll(self, win, _deltax, deltay):
        """ Scroll controls the camera distance to trackball center """
        self.trackball.zoom(deltay, glfw.get_window_size(win)[1])

    def on_size(self, win, _width, _height):
        """ window size update => update viewport to new framebuffer size """
        GL.glViewport(0, 0, *glfw.get_framebuffer_size(win))


class Texture:
    """ Helper class to create and automatically destroy textures """

    def __init__(self, tex_file, wrap_mode=GL.GL_REPEAT, min_filter=GL.GL_LINEAR,
                 mag_filter=GL.GL_LINEAR_MIPMAP_LINEAR):
        self.glid = GL.glGenTextures(1)
        try:
            # imports image as a numpy array in exactly right format
            tex = np.asarray(Image.open(tex_file).convert('RGBA'))
            GL.glBindTexture(GL.GL_TEXTURE_2D, self.glid)
            GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGBA, tex.shape[1],
                            tex.shape[0], 0, GL.GL_RGBA, GL.GL_UNSIGNED_BYTE, tex)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_S, wrap_mode)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_T, wrap_mode)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, min_filter)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, mag_filter)
            GL.glGenerateMipmap(GL.GL_TEXTURE_2D)
            message = 'Loaded texture %s\t(%s, %s, %s, %s)'
            print(message % (tex_file, tex.shape, wrap_mode, min_filter, mag_filter))
        except FileNotFoundError:
            print("ERROR: unable to load texture file %s" % tex_file)

    # def __del__(self):  # delete GL texture from GPU when object dies
    #     GL.glDeleteTextures(self.glid)


# -------------- Example texture plane class ----------------------------------
class TexturedPlane(Mesh):
    """ Simple first textured object """

    def __init__(self, background_texture_file, road_texture_file, blendmap_file, shader, size, hmap_file):

        # Load heightmap file
        hmap_tex = np.asarray(Image.open(hmap_file).convert('RGB'))

        self.MAX_HEIGHT = 30
        self.MIN_HEIGHT = 0
        self.MAX_PIXEL_COLOR = 256
        self.HMAP_SIZE = hmap_tex.shape[0]  # 256
        self.background_texture_file = background_texture_file
        self.road_texture_file = road_texture_file
        self.blendmap_file = blendmap_file
        self.fog_colour = FogColour()

        vertices, texture_coords, normals, indices = self.create_attributes(self.HMAP_SIZE, hmap_tex=hmap_tex)

        super().__init__(shader, [vertices, texture_coords, normals], indices)

        self.names = ['diffuse_map', 'blue_texture', 'blendmap', 'fog_colour']
        self.loc1 = {n: GL.glGetUniformLocation(shader.glid, n) for n in self.names}

        # interactive toggles
        self.wrap = cycle([GL.GL_REPEAT, GL.GL_MIRRORED_REPEAT,
                           GL.GL_CLAMP_TO_BORDER, GL.GL_CLAMP_TO_EDGE])
        self.filter = cycle([(GL.GL_NEAREST, GL.GL_NEAREST),
                             (GL.GL_LINEAR, GL.GL_LINEAR),
                             (GL.GL_LINEAR, GL.GL_LINEAR_MIPMAP_LINEAR)])
        self.wrap_mode, self.filter_mode = next(self.wrap), next(self.filter)

        # setup texture and upload it to GPU
        self.background_texture = Texture(self.background_texture_file, self.wrap_mode, *self.filter_mode)
        self.road_texture = Texture(self.road_texture_file, self.wrap_mode, *self.filter_mode)
        self.blendmap_texture = Texture(self.blendmap_file, self.wrap_mode, *self.filter_mode)

    def create_attributes(self, size, hmap_tex):
        vertices = []
        normals = []
        texture_coords = []

        # Create vertices, normals, and texture coordinates
        for i in range(0, size):
            for j in range(0, size):
                # Vertices - (x, y, z)
                vertices.append([(j / (size - 1)) * 1000,
                                 self.get_height(i, j, image=hmap_tex),
                                 (i / (size - 1)) * 1000])
                # print(self.get_height(i, j, image=hmap_tex))
                normals.append([0, 1, 0])
                texture_coords.append([j / (size - 1), i / (size - 1)])

        # Convert to numpy array list
        vertices = np.array(vertices)
        normals = np.array(normals)
        texture_coords = np.array(texture_coords)

        indices = []
        for gz in range(0, size - 1):
            for gx in range(0, size - 1):
                top_left = (gz * size) + gx
                top_right = top_left + 1
                bottom_left = ((gz + 1) * size) + gx
                bottom_right = bottom_left + 1
                indices.append([top_left, bottom_left, top_right, top_right, bottom_left, bottom_right])

        indices = np.array(indices)

        return vertices, texture_coords, normals, indices

    def get_height(self, x, z, image):
        if x < 0 or x >= image.shape[0] or z < 0 or z >= image.shape[0]:
            return 0
        height = image[x, z, 0]
        # [0 to 1] range
        height /= self.MAX_PIXEL_COLOR
        # [0 to MAX_HEIGHT] range
        height *= self.MAX_HEIGHT

        return height

    def key_handler(self, key):
        # some interactive elements
        if key == glfw.KEY_F6:
            self.wrap_mode = next(self.wrap)
            self.texture = Texture(self.background_texture_file, self.wrap_mode, *self.filter_mode)
        if key == glfw.KEY_F7:
            self.filter_mode = next(self.filter)
            self.texture = Texture(self.background_texture_file, self.wrap_mode, *self.filter_mode)

    def draw(self, projection, view, model, primitives=GL.GL_TRIANGLES):
        GL.glUseProgram(self.shader.glid)

        # texture access setups
        self.bind_textures()
        self.connect_texture_units()
        super().draw(projection, view, model, primitives)

    def connect_texture_units(self):
        GL.glUniform1i(self.loc1['diffuse_map'], 0)
        GL.glUniform1i(self.loc1['blue_texture'], 1)
        GL.glUniform1i(self.loc1['blendmap'], 2)
        GL.glUniform3fv(self.loc1['fog_colour'], 1, self.fog_colour.get_colour())

    def bind_textures(self):
        GL.glActiveTexture(GL.GL_TEXTURE0)
        GL.glBindTexture(GL.GL_TEXTURE_2D, self.background_texture.glid)
        GL.glActiveTexture(GL.GL_TEXTURE1)
        GL.glBindTexture(GL.GL_TEXTURE_2D, self.road_texture.glid)
        GL.glActiveTexture(GL.GL_TEXTURE2)
        GL.glBindTexture(GL.GL_TEXTURE_2D, self.blendmap_texture.glid)


class TexturedMesh(Mesh):

    def __init__(self, shader, tex, attributes, faces):
        super().__init__(shader, attributes, faces)

        loc = GL.glGetUniformLocation(shader.glid, 'diffuse_map')
        self.loc['diffuse_map'] = loc

        # setup texture and upload it to GPU
        self.texture = tex

    def draw(self, projection, view, model, primitives=GL.GL_TRIANGLES):
        GL.glUseProgram(self.shader.glid)

        # texture access setups
        GL.glActiveTexture(GL.GL_TEXTURE0)
        GL.glBindTexture(GL.GL_TEXTURE_2D, self.texture.glid)
        GL.glUniform1i(self.loc['diffuse_map'], 0)
        super().draw(projection, view, model, primitives)

        # leave clean state for easier debugging
        GL.glBindTexture(GL.GL_TEXTURE_2D, 0)
        GL.glUseProgram(0)


# -------------- Phong rendered Mesh class -----------------------------------
class PhongMesh(Mesh):
    """ Mesh with Phong illumination """

    def __init__(self, shader, attributes, index=None,
                 light_dir=(1, -1, 1),  # directional light (in world coords)
                 k_a=(0, 0, 0), k_d=(1, 1, 0), k_s=(1, 1, 1), s=16.):
        super().__init__(shader, attributes, index)

        print(light_dir)
        self.light_dir = light_dir
        self.k_a, self.k_d, self.k_s, self.s = k_a, k_d, k_s, s

        # retrieve OpenGL locations of shader variables at initialization
        names = ['light_dir', 'k_a', 's', 'k_s', 'k_d', 'w_camera_position']

        loc = {n: GL.glGetUniformLocation(shader.glid, n) for n in names}
        self.loc.update(loc)

    def draw(self, projection, view, model, primitives=GL.GL_TRIANGLES):
        GL.glUseProgram(self.shader.glid)

        # setup light parameters
        GL.glUniform3fv(self.loc['light_dir'], 1, self.light_dir)

        # setup material parameters
        GL.glUniform3fv(self.loc['k_a'], 1, self.k_a)
        GL.glUniform3fv(self.loc['k_d'], 1, self.k_d)
        GL.glUniform3fv(self.loc['k_s'], 1, self.k_s)
        GL.glUniform1f(self.loc['s'], max(self.s, 0.001))

        # world camera position for Phong illumination specular component
        w_camera_position = np.linalg.inv(view)[:, 3]
        GL.glUniform3fv(self.loc['w_camera_position'], 1, w_camera_position)

        super().draw(projection, view, model, primitives)


class TexturedPhongMesh:
    def __init__(self, shader, tex, attributes, faces,
                 light_dir=None,  # directional light (in world coords)
                 k_a=(1, 1, 1), k_d=(1, 1, 0), k_s=(1, 1, 0), s=64.
                 ):
        # super().__init__(shader, tex, attributes, faces)

        # setup texture and upload it to GPU
        self.texture = tex
        self.vertex_array = VertexArray(attributes=attributes, index=faces)
        self.shader = shader
        self.fog_colour = FogColour()

        self.k_a = k_a
        self.k_d = k_d
        self.k_s = k_s
        self.s = s
        # ----------------

    def draw(self, projection, view, model, primitives=GL.GL_TRIANGLES):
        GL.glUseProgram(self.shader.glid)

        # projection geometry
        names = ['view', 'projection', 'model', 'nit_matrix', 'diffuseMap', 'k_a', 'k_d', 'k_s', 's', 'fog_colour']
        loc = {n: GL.glGetUniformLocation(self.shader.glid, n) for n in names}

        # model3x3 = model[0:3, 0:3]
        # nit_matrix = np.linalg.inv(model3x3).T

        GL.glUniformMatrix4fv(loc['view'], 1, True, view)
        GL.glUniformMatrix4fv(loc['projection'], 1, True, projection)
        GL.glUniformMatrix4fv(loc['model'], 1, True, model)

        GL.glUniform3fv(loc['k_a'], 1, self.k_a)
        GL.glUniform3fv(loc['k_d'], 1, self.k_d)
        GL.glUniform3fv(loc['k_s'], 1, self.k_s)
        GL.glUniform1f(loc['s'], max(self.s, 0.001))
        GL.glUniform3fv(loc['fog_colour'], 1, self.fog_colour.get_colour())
        # GL.glUniformMatrix4fv(loc['nit_matrix'], 1, True, nit_matrix)

        # ----------------
        # texture access setups
        # loc = GL.glGetUniformLocation(self.shader.glid, 'diffuseMap')
        GL.glActiveTexture(GL.GL_TEXTURE0)
        GL.glBindTexture(GL.GL_TEXTURE_2D, self.texture.glid)
        GL.glUniform1i(loc['diffuseMap'], 0)
        self.vertex_array.execute(primitives)

        # leave clean state for easier debugging
        GL.glBindTexture(GL.GL_TEXTURE_2D, 0)
        GL.glUseProgram(0)


class TexturedPhongMeshSkinned:
    def __init__(self, shader, tex, attributes, faces,
                 bone_nodes, bone_offsets,
                 light_dir=None,  # directional light (in world coords)
                 k_a=(1, 1, 1), k_d=(1, 1, 0), k_s=(1, 1, 0), s=64.
                 ):
        # super().__init__(shader, tex, attributes, faces)

        # setup texture and upload it to GPU
        self.texture = tex
        self.vertex_array = VertexArray(attributes=attributes, index=faces)
        self.shader = shader
        self.fog_colour = FogColour()

        self.k_a = k_a
        self.k_d = k_d
        self.k_s = k_s
        self.s = s
        # ----------------
        self.bone_nodes = bone_nodes
        self.bone_offsets = bone_offsets

    def draw(self, projection, view, model, primitives=GL.GL_TRIANGLES):
        GL.glUseProgram(self.shader.glid)

        # projection geometry
        names = ['view', 'projection', 'model', 'nit_matrix', 'diffuseMap', 'k_a', 'k_d', 'k_s', 's',
                 'fog_colour', 'w_camera_position']
        loc = {n: GL.glGetUniformLocation(self.shader.glid, n) for n in names}

        # model3x3 = model[0:3, 0:3]
        # nit_matrix = np.linalg.inv(model3x3).T

        GL.glUniformMatrix4fv(loc['view'], 1, True, view)
        GL.glUniformMatrix4fv(loc['projection'], 1, True, projection)
        GL.glUniformMatrix4fv(loc['model'], 1, True, model)

        GL.glUniform3fv(loc['k_a'], 1, self.k_a)
        GL.glUniform3fv(loc['k_d'], 1, self.k_d)
        GL.glUniform3fv(loc['k_s'], 1, self.k_s)
        GL.glUniform1f(loc['s'], max(self.s, 0.001))
        GL.glUniform3fv(loc['fog_colour'], 1, self.fog_colour.get_colour())
        # GL.glUniformMatrix4fv(loc['nit_matrix'], 1, True, nit_matrix)

        # bone world transform matrices need to be passed for skinning
        for bone_id, node in enumerate(self.bone_nodes):
            bone_matrix = node.world_transform @ self.bone_offsets[bone_id]

            bone_loc = GL.glGetUniformLocation(self.texture.glid, 'bone_matrix[%d]' % bone_id)
            GL.glUniformMatrix4fv(bone_loc, 1, True, bone_matrix)

        # world camera position for Phong illumination specular component
        w_camera_position = np.linalg.inv(view)[:, 3]
        GL.glUniform3fv(loc['w_camera_position'], 1, w_camera_position)

        # ----------------
        # texture access setups
        # loc = GL.glGetUniformLocation(self.shader.glid, 'diffuseMap')
        GL.glActiveTexture(GL.GL_TEXTURE0)
        GL.glBindTexture(GL.GL_TEXTURE_2D, self.texture.glid)
        GL.glUniform1i(loc['diffuseMap'], 0)
        self.vertex_array.execute(primitives)

        # leave clean state for easier debugging
        GL.glBindTexture(GL.GL_TEXTURE_2D, 0)
        GL.glUseProgram(0)


# ------------------------ PhoneMesh loader -------------------------------

def load_textured_phong_mesh(file, shader, tex_file, k_a, k_d, k_s, s):
    try:
        pp = assimpcy.aiPostProcessSteps
        flags = pp.aiProcess_Triangulate | pp.aiProcess_FlipUVs
        scene = assimpcy.aiImportFile(file, flags)
    except assimpcy.all.AssimpError as exception:
        print('ERROR loading', file + ': ', exception.args[0].decode())
        return []

    # Note: embedded textures not supported at the moment
    path = os.path.dirname(file) if os.path.dirname(file) != '' else './'
    for mat in scene.mMaterials:
        if not tex_file and 'TEXTURE_BASE' in mat.properties:  # texture token
            name = os.path.basename(mat.properties['TEXTURE_BASE'])
            # search texture in file's whole subdir since path often screwed up
            paths = os.walk(path, followlinks=True)
            found = [os.path.join(d, f) for d, _, n in paths for f in n
                     if name.startswith(f) or f.startswith(name)]
            assert found, 'Cannot find texture %s in %s subtree' % (name, path)
            tex_file = found[0]
        if tex_file:
            mat.properties['diffuse_map'] = Texture(tex_file=tex_file)

    meshes = []
    for mesh in scene.mMeshes:
        mat = scene.mMaterials[mesh.mMaterialIndex].properties
        assert mat['diffuse_map'], "Trying to map using a textureless material"
        attributes = [mesh.mVertices, mesh.mTextureCoords[0], mesh.mNormals]
        mesh = TexturedPhongMesh(shader=shader, tex=mat['diffuse_map'], attributes=attributes,
                                 faces=mesh.mFaces,
                                 k_d=k_d, k_a=k_a, k_s=k_s, s=s)
        meshes.append(mesh)

        size = sum((mesh.mNumFaces for mesh in scene.mMeshes))
        print('Loaded %s\t(%d meshes, %d faces)' % (file, len(meshes), size))
        return meshes


def load_textured_phong_mesh_skinned(file, shader, tex_file, k_a, k_d, k_s, s):
    try:
        pp = assimpcy.aiPostProcessSteps
        flags = pp.aiProcess_Triangulate | pp.aiProcess_FlipUVs
        scene = assimpcy.aiImportFile(file, flags)
    except assimpcy.all.AssimpError as exception:
        print('ERROR loading', file + ': ', exception.args[0].decode())
        return []

    # Note: embedded textures not supported at the moment
    path = os.path.dirname(file) if os.path.dirname(file) != '' else './'
    for mat in scene.mMaterials:
        if not tex_file and 'TEXTURE_BASE' in mat.properties:  # texture token
            name = os.path.basename(mat.properties['TEXTURE_BASE'])
            # search texture in file's whole subdir since path often screwed up
            paths = os.walk(path, followlinks=True)
            found = [os.path.join(d, f) for d, _, n in paths for f in n
                     if name.startswith(f) or f.startswith(name)]
            assert found, 'Cannot find texture %s in %s subtree' % (name, path)
            tex_file = found[0]
        if tex_file:
            mat.properties['diffuse_map'] = Texture(tex_file=tex_file)

    # ----- load animations
    def conv(assimp_keys, ticks_per_second):
        """ Conversion from assimp key struct to our dict representation """
        return {key.mTime / ticks_per_second: key.mValue for key in assimp_keys}

    # load first animation in scene file (could be a loop over all animations)
    transform_keyframes = {}
    if scene.mAnimations:
        anim = scene.mAnimations[0]
        for channel in anim.mChannels:
            # for each animation bone, store TRS dict with {times: transforms}
            transform_keyframes[channel.mNodeName] = (
                conv(channel.mPositionKeys, anim.mTicksPerSecond),
                conv(channel.mRotationKeys, anim.mTicksPerSecond),
                conv(channel.mScalingKeys, anim.mTicksPerSecond)
            )

    # ---- prepare scene graph nodes
    # create SkinningControlNode for each assimp node.
    # node creation needs to happen first as SkinnedMeshes store an array of
    # these nodes that represent their bone transforms
    nodes = {}  # nodes name -> node lookup
    nodes_per_mesh_id = [[] for _ in scene.mMeshes]  # nodes holding a mesh_id

    def make_nodes(assimp_node):
        """ Recursively builds nodes for our graph, matching assimp nodes """
        trs_keyframes = transform_keyframes.get(assimp_node.mName, (None,))
        skin_node = SkinningControlNode(*trs_keyframes,
                                        transform=assimp_node.mTransformation)
        nodes[assimp_node.mName] = skin_node
        for mesh_index in assimp_node.mMeshes:
            nodes_per_mesh_id[mesh_index].append(skin_node)
        skin_node.add(*(make_nodes(child) for child in assimp_node.mChildren))
        return skin_node

    root_node = make_nodes(scene.mRootNode)

    # ---- create SkinnedMesh objects
    for mesh_id, mesh in enumerate(scene.mMeshes):
        # -- skinned mesh: weights given per bone => convert per vertex for GPU
        # first, populate an array with MAX_BONES entries per vertex
        v_bone = np.array([[(0, 0)] * MAX_BONES] * mesh.mNumVertices,
                          dtype=[('weight', 'f4'), ('id', 'u4')])
        for bone_id, bone in enumerate(mesh.mBones[:MAX_BONES]):
            for entry in bone.mWeights:  # weight,id pairs necessary for sorting
                v_bone[entry.mVertexId][bone_id] = (entry.mWeight, bone_id)

        v_bone.sort(order='weight')  # sort rows, high weights last
        v_bone = v_bone[:, -MAX_VERTEX_BONES:]  # limit bone size, keep highest

        # prepare bone lookup array & offset matrix, indexed by bone index (id)
        bone_nodes = [nodes[bone.mName] for bone in mesh.mBones]
        bone_offsets = [bone.mOffsetMatrix for bone in mesh.mBones]

        # Initialize mat for phong and texture
        mat = scene.mMaterials[mesh.mMaterialIndex].properties
        assert mat['diffuse_map'], "Trying to map using a textureless material"

    meshes = []
    for mesh in scene.mMeshes:
        mat = scene.mMaterials[mesh.mMaterialIndex].properties
        assert mat['diffuse_map'], "Trying to map using a textureless material"
        attributes = [mesh.mVertices, mesh.mTextureCoords[0], mesh.mNormals, v_bone['id'], v_bone['weight']]
        mesh = TexturedPhongMeshSkinned(shader=shader, tex=mat['diffuse_map'], attributes=attributes,
                                        faces=mesh.mFaces, bone_nodes=bone_nodes, bone_offsets=bone_offsets,
                                        k_d=k_d, k_a=k_a, k_s=k_s, s=s)
        # meshes.append(mesh)

        for node in nodes_per_mesh_id[mesh_id]:
            node.add(mesh)

        nb_triangles = sum((mesh.mNumFaces for mesh in scene.mMeshes))
        print('Loaded', file, '\t(%d meshes, %d faces, %d nodes, %d animations)' % (
            scene.mNumMeshes, nb_triangles, len(nodes), scene.mNumAnimations))
        return meshes

    return [root_node]


def load_phong_mesh(file, shader, light_dir, tex_file):
    """ load resources from file using assimp, return list of ColorMesh """
    try:
        pp = assimpcy.aiPostProcessSteps
        flags = pp.aiProcess_Triangulate | pp.aiProcess_GenSmoothNormals
        scene = assimpcy.aiImportFile(file, flags)
    except assimpcy.all.AssimpError as exception:
        print('ERROR loading', file + ': ', exception.args[0].decode())
        return []

    # prepare mesh nodes
    meshes = []
    for mesh in scene.mMeshes:
        mat = scene.mMaterials[mesh.mMaterialIndex].properties
        mesh = PhongMesh(shader, [mesh.mVertices, mesh.mNormals], mesh.mFaces,
                         k_d=mat.get('COLOR_DIFFUSE', (1, 1, 1)),
                         k_s=mat.get('COLOR_SPECULAR', (1, 1, 1)),
                         k_a=mat.get('COLOR_AMBIENT', (0, 0, 0)),
                         s=mat.get('SHININESS', 16.),
                         light_dir=light_dir)
        meshes.append(mesh)

    size = sum((mesh.mNumFaces for mesh in scene.mMeshes))
    print('Loaded %s\t(%d meshes, %d faces)' % (file, len(meshes), size))
    return meshes


class FogColour:
    def __init__(self):
        self.colour = [0, 0, 0]
        self.time = 0
        self.factor = (1 / 6000) * 14
        self.day_color = [0.6, 0.7, 0.7]
        self.night_colour = [0.2, 0.3, 0.3]

    def get_colour(self):
        self.time = glfw.get_time() * 1000
        self.time %= 24000
        if 0 <= self.time < 6000:
            self.colour = [0.2, 0.3, 0.3]
            # print(self.colour)

        elif 6000 <= self.time < 12000:
            if self.colour[0] < self.day_color[0]:
                self.colour[0] += self.factor
                self.colour[1] += self.factor
                self.colour[2] += self.factor
                # print(self.colour)

        elif 12000 <= self.time < 18000:
            self.colour = [0.6, 0.7, 0.7]
            # print(self.colour)

        else:
            if self.colour[0] > self.night_colour[0]:
                self.colour[0] -= self.factor
                self.colour[1] -= self.factor
                self.colour[2] -= self.factor
                # # print(self.colour)

        return self.colour


class KeyFrames:
    """ Stores keyframe pairs for any value type with interpolation_function"""

    def __init__(self, time_value_pairs, interpolation_function=lerp):
        if isinstance(time_value_pairs, dict):  # convert to list of pairs
            time_value_pairs = time_value_pairs.items()
        keyframes = sorted(((key[0], key[1]) for key in time_value_pairs))
        self.times, self.values = zip(*keyframes)  # pairs list -> 2 lists
        self.interpolate = interpolation_function

    def value(self, time):
        """ Computes interpolated value from keyframes, for a given time """

        # 1. ensure time is within bounds else return boundary keyframe
        if time <= self.times[0]:
            return self.values[0]
        elif time >= self.times[len(self.times) - 1]:
            return self.values[len(self.times) - 1]

        # 2. search for closest index entry in self.times, using bisect_left function
        index_closest = bisect_left(self.times, time)

        # 3. using the retrieved index, interpolate between the two neighboring values
        # in self.values, using the initially stored self.interpolate function
        f = (time - self.times[index_closest - 1]) / (self.times[index_closest] - self.times[index_closest - 1])

        interpolated_val = self.interpolate(self.values[index_closest], self.values[index_closest - 1], f)
        return interpolated_val


class TransformKeyFrames:
    """ KeyFrames-like object dedicated to 3D transforms """

    def __init__(self, translate_keys, rotate_keys, scale_keys):
        """ stores 3 keyframe sets for translation, rotation, scale """
        self.translate_keys = KeyFrames(translate_keys)
        self.rotate_keys = KeyFrames(rotate_keys, interpolation_function=quaternion_slerp)
        self.scale_keys = KeyFrames(scale_keys)

    def value(self, time):
        """ Compute each component's interpolation and compose TRS matrix """
        T = translate(self.translate_keys.value(time=time))
        R = quaternion_matrix(self.rotate_keys.value(time=time))
        S = scale(self.scale_keys.value(time=time))
        return T @ R @ S


class KeyFrameControlNode(Node):
    """ Place node with transform keys above a controlled subtree """

    def __init__(self, translate_keys, rotate_keys, scale_keys):
        super().__init__()
        self.keyframes = TransformKeyFrames(translate_keys, rotate_keys, scale_keys)

    def draw(self, projection, view, model):
        """ When redraw requested, interpolate our node transform from keys """
        self.transform = self.keyframes.value(glfw.get_time())
        super().draw(projection, view, model)


# -------------- Linear Blend Skinning : TP7 ---------------------------------
MAX_VERTEX_BONES = 4
MAX_BONES = 128


class SkinnedMesh(Mesh):
    """class of skinned mesh nodes in scene graph """

    def __init__(self, shader, attribs, bone_nodes, bone_offsets, index=None):
        super().__init__(shader, attribs, index)

        # store skinning data
        self.bone_nodes = bone_nodes
        self.bone_offsets = np.array(bone_offsets, np.float32)

    def draw(self, projection, view, model, primitives=GL.GL_TRIANGLES):
        """ skinning object draw method """
        GL.glUseProgram(self.shader.glid)

        # bone world transform matrices need to be passed for skinning
        world_transforms = [node.world_transform for node in self.bone_nodes]
        bone_matrix = world_transforms @ self.bone_offsets
        loc = GL.glGetUniformLocation(self.shader.glid, 'bone_matrix')
        GL.glUniformMatrix4fv(loc, len(self.bone_nodes), True, bone_matrix)

        super().draw(projection, view, model)


# -------- Skinning Control for Keyframing Skinning Mesh Bone Transforms ------
class SkinningControlNode(Node):
    """ Place node with transform keys above a controlled subtree """

    def __init__(self, *keys, transform=identity()):
        super().__init__(transform=transform)
        self.keyframes = TransformKeyFrames(*keys) if keys[0] else None
        self.world_transform = identity()

    def draw(self, projection, view, model):
        """ When redraw requested, interpolate our node transform from keys """
        if self.keyframes:  # no keyframe update should happens if no keyframes
            self.transform = self.keyframes.value(glfw.get_time())

        # store world transform for skinned meshes using this node as bone
        self.world_transform = model @ self.transform

        # default node behaviour (call children's draw method)
        super().draw(projection, view, model)

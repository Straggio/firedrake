from firedrake import *
from firedrake.plot import (triplot, tricontour, tricontourf, tripcolor,
                            trisurf, quiver)
import matplotlib.pyplot as plt


def test_plotting_scalar_field():
    mesh = UnitSquareMesh(10, 10)
    V = FunctionSpace(mesh, "CG", 1)
    f = Function(V)
    x = SpatialCoordinate(mesh)
    f.interpolate(x[0] + x[1])

    # Plot without first creating axes
    contours = tricontour(f)

    # Create axes first then plot
    fig, axes = plt.subplots(ncols=3, sharex=True, sharey=True)
    contours = tricontour(f, axes=axes[0])
    assert contours is not None
    assert not contours.filled
    fig.colorbar(contours, ax=axes[0])

    filled_contours = tricontourf(f, axes=axes[1])
    assert filled_contours is not None
    assert filled_contours.filled
    fig.colorbar(filled_contours, ax=axes[1])

    collection = tripcolor(f, axes=axes[2])
    assert collection is not None
    fig.colorbar(collection, ax=axes[2])


def test_plotting_quadratic():
    mesh = UnitSquareMesh(10, 10)
    V = FunctionSpace(mesh, "CG", 2)
    f = Function(V)
    x = SpatialCoordinate(mesh)
    f.interpolate(x[0] ** 2 + x[1] ** 2)

    fig, axes = plt.subplots()
    contours = tricontour(f, axes=axes)
    assert contours is not None


def test_tricontour_quad_mesh():
    mesh = UnitSquareMesh(10, 10, quadrilateral=True)
    V = FunctionSpace(mesh, "CG", 1)
    f = Function(V)
    x = SpatialCoordinate(mesh)
    f.interpolate(x[0] ** 2 + x[1] ** 2)

    fig, axes = plt.subplots()
    contours = tricontourf(f, axes=axes)
    colorbar = fig.colorbar(contours)
    assert contours is not None
    assert colorbar is not None


def test_quiver_plot():
    mesh = UnitSquareMesh(10, 10)
    V = VectorFunctionSpace(mesh, "CG", 1)
    f = Function(V)
    x = SpatialCoordinate(mesh)
    f.interpolate(as_vector((-x[1], x[0])))

    fig, axes = plt.subplots()
    arrows = quiver(f, axes=axes)
    assert arrows is not None
    fig.colorbar(arrows)


def test_plotting_vector_field():
    mesh = UnitSquareMesh(10, 10)
    V = VectorFunctionSpace(mesh, "CG", 1)
    f = Function(V)
    x = SpatialCoordinate(mesh)
    f.interpolate(as_vector((-x[1], x[0])))

    fig, axes = plt.subplots()
    contours = tricontourf(f, axes=axes)
    assert contours is not None
    fig.colorbar(contours)


def test_triplot():
    mesh = UnitSquareMesh(10, 10)
    fig, axes = plt.subplots(ncols=2, sharex=True, sharey=True)
    lines = triplot(mesh, axes=axes[0])
    assert lines is not None
    legend = axes[0].legend(loc='upper right')
    assert legend is not None

    colors = ['tab:blue', 'tab:orange', 'tab:green', 'tab:red']
    lines = triplot(mesh, axes=axes[1], linewidth=0.5,
                    boundary_linewidth=2.0, boundary_colors=colors)


def test_triplot_quad_mesh():
    mesh = UnitSquareMesh(10, 10, quadrilateral=True)
    fig, axes = plt.subplots()
    lines = triplot(mesh, axes=axes)
    assert lines is not None
    legend = axes.legend(loc='upper right')
    assert legend is not None


def test_3d_surface_plot():
    from mpl_toolkits.mplot3d import Axes3D
    mesh = UnitSquareMesh(10, 10)
    V = FunctionSpace(mesh, "CG", 2)
    f = Function(V)
    x = SpatialCoordinate(mesh)
    f.interpolate(x[0] ** 2 + x[1] ** 2)

    fig = plt.figure()
    axes = fig.add_subplot(projection='3d')
    assert isinstance(axes, Axes3D)
    collection = trisurf(f, axes=axes)
    assert collection is not None

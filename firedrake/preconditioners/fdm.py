from functools import lru_cache, partial

from pyop2.sparsity import get_preallocation

import ufl
from ufl import inner, diff
from ufl.constantvalue import Zero
from ufl.algorithms.ad import expand_derivatives

from firedrake.petsc import PETSc
from firedrake.preconditioners.base import PCBase
from firedrake.preconditioners.patch import bcdofs
from firedrake.preconditioners.pmg import get_shift, get_line_elements, prolongation_matrix_matfree
import firedrake.dmhooks as dmhooks
from firedrake.dmhooks import get_function_space, get_appctx
import firedrake
import numpy
from firedrake_citations import Citations

Citations().add("Brubeck2021", """
@misc{Brubeck2021,
  title={A scalable and robust vertex-star relaxation for high-order {FEM}},
  author={Brubeck, Pablo D. and Farrell, Patrick E.},
  archiveprefix = {arXiv},
  eprint = {2107.14758},
  primaryclass = {math.NA},
  year={2021}
}
""")

__all__ = ("FDMPC",)


class FDMPC(PCBase):
    """
    A preconditioner for tensor-product elements that changes the shape
    functions so that the H^1 Riesz map is diagonalized in the interior of a
    Cartesian cell, and assembles a global sparse matrix on which other
    preconditioners, such as `ASMStarPC`, can be applied.

    Here we assume that the volume integrals in the Jacobian can be expressed as:

    inner(grad(v), alpha(grad(u)))*dx + inner(v, beta(u))*dx

    where alpha and beta are linear functions (tensor contractions).
    The sparse matrix is obtained by approximating alpha and beta by cell-wise
    constants and discarding the coefficients in alpha that couple together
    mixed derivatives and mixed components.

    For spaces that are not H^1-conforming, this preconditioner will use
    the symmetric interior-penalty DG method. The penalty coefficient can be
    provided in the application context, keyed on ``"eta"``.
    """

    _prefix = "fdm_"

    def initialize(self, pc):
        from firedrake.assemble import allocate_matrix, assemble
        Citations().register("Brubeck2021")

        prefix = pc.getOptionsPrefix()
        options_prefix = prefix + self._prefix

        appctx = self.get_appctx(pc)
        fcp = appctx.get("form_compiler_parameters")

        # Get original Jacobian form and bcs
        dm = pc.getDM()
        octx = get_appctx(dm)
        mat_type = octx.mat_type
        oproblem = octx._problem
        J = oproblem.J
        bcs = tuple(oproblem.bcs)

        # Transform the problem into the space with FDM shape functions
        V = get_function_space(dm)
        element = V.ufl_element()
        e_fdm = element.reconstruct(variant="fdm")

        # Matrix-free assembly of the transformed Jacobian and its diagonal
        if element == e_fdm:
            V_fdm, J_fdm, bcs_fdm = (V, J, bcs)
            Amat, _ = pc.getOperators()
            self._ctx_ref = octx
        else:
            V_fdm = firedrake.FunctionSpace(V.mesh(), e_fdm)
            J_fdm = ufl.replace(J, {t: t.reconstruct(function_space=V_fdm) for t in J.arguments()})
            bcs_fdm = tuple(bc.reconstruct(V=V_fdm) for bc in bcs)
            self.fdm_interp = prolongation_matrix_matfree(V, V_fdm, [], bcs_fdm)
            self.A = allocate_matrix(J_fdm, bcs=bcs_fdm, form_compiler_parameters=fcp, mat_type=mat_type,
                                     options_prefix=options_prefix)
            self._assemble_A = partial(assemble, J_fdm, tensor=self.A, bcs=bcs_fdm,
                                       form_compiler_parameters=fcp, mat_type=mat_type,
                                       assembly_type="residual")
            self._assemble_A()
            Amat = self.A.petscmat
            self._ctx_ref = self.new_snes_ctx(pc, J_fdm, bcs_fdm, mat_type,
                                              fcp=fcp, options_prefix=options_prefix)

        self.work = firedrake.Function(V_fdm)
        self.diag = firedrake.Function(V_fdm)
        self._assemble_diag = partial(assemble, J_fdm, tensor=self.diag, bcs=bcs_fdm,
                                      diagonal=True, form_compiler_parameters=fcp, mat_type=mat_type)

        if len(bcs) > 0:
            self.bc_nodes = numpy.unique(numpy.concatenate([bcdofs(bc, ghost=False) for bc in bcs]))
        else:
            self.bc_nodes = numpy.empty(0, dtype=PETSc.IntType)

        # Assemble the FDM preconditioner with sparse local matrices
        Pmat, self._assemble_P = self.assemble_fdm_op(V_fdm, J_fdm, bcs_fdm, appctx)
        self._assemble_P()

        # Internally, we just set up a PC object that the user can configure
        # however from the PETSc command line.  Since PC allows the user to specify
        # a KSP, we can do iterative by -fdm_pc_type ksp.
        fdmpc = PETSc.PC().create(comm=pc.comm)
        fdmpc.incrementTabLevel(1, parent=pc)

        # We set a DM and an appropriate SNESContext on the constructed PC so one
        # can do e.g. multigrid or patch solves.
        fdm_dm = V_fdm.dm
        self._dm = fdm_dm

        fdmpc.setDM(fdm_dm)
        fdmpc.setOptionsPrefix(options_prefix)
        fdmpc.setOperators(A=Amat, P=Pmat)
        fdmpc.setUseAmat(True)
        self.pc = fdmpc

        with dmhooks.add_hooks(fdm_dm, self, appctx=self._ctx_ref, save=False):
            fdmpc.setFromOptions()
        # self.diagonal_scaling()

    def update(self, pc):
        if hasattr(self, "A"):
            self._assemble_A()
        self._assemble_P()
        # self.diagonal_scaling()

    def apply(self, pc, x, y):
        dm = self._dm
        with dmhooks.add_hooks(dm, self, appctx=self._ctx_ref):
            if hasattr(self, "fdm_interp"):
                with self.work.dat.vec as x_fdm, self.diag.dat.vec as y_fdm:
                    self.fdm_interp.multTranspose(x, x_fdm)
                    self.pc.apply(x_fdm, y_fdm)
                    self.fdm_interp.mult(y_fdm, y)
                y.array_w[self.bc_nodes] = x.array_r[self.bc_nodes]
            else:
                self.pc.apply(x, y)

    def applyTranspose(self, pc, x, y):
        dm = self._dm
        with dmhooks.add_hooks(dm, self, appctx=self._ctx_ref):
            if hasattr(self, "fdm_interp"):
                with self.work.dat.vec as x_fdm, self.diag.dat.vec as y_fdm:
                    self.fdm_interp.multTranspose(x, x_fdm)
                    self.pc.applyTranspose(x_fdm, y_fdm)
                    self.fdm_interp.mult(y_fdm, y)
                y.array_w[self.bc_nodes] = x.array_r[self.bc_nodes]
            else:
                self.pc.applyTranspose(x, y)

    def view(self, pc, viewer=None):
        super(FDMPC, self).view(pc, viewer)
        if hasattr(self, "pc"):
            viewer.printfASCII("PC to apply inverse\n")
            self.pc.view(viewer)

    def diagonal_scaling(self):
        _, P = self.pc.getOperators()
        self._assemble_diag()
        with self.diag.dat.vec as x_, self.work.dat.vec as y_:
            P.getDiagonal(y_)
            x_.pointwiseDivide(x_, y_)
            x_.sqrtabs()
            P.diagonalScale(L=x_, R=x_)

    def assemble_fdm_op(self, V, J, bcs, appctx):
        """
        Assemble the sparse preconditioner with cell-wise constant coefficients.

        :arg V: the :class:`firedrake.FunctionSpace` of the form arguments
        :arg J: the Jacobian bilinear form
        :arg bcs: an iterable of boundary conditions on V
        :arg appctx: the application context

        :returns: 2-tuple with the preconditioner :class:`PETSc.Mat` and its assembly callable
        """
        element = V.finat_element
        is_dg = element.entity_dofs() == element.entity_closure_dofs()
        element = V.ufl_element()
        degree = element.degree()
        try:
            degree = max(degree)
        except TypeError:
            pass
        eta = float(appctx.get("eta", (degree+1)**2))
        quad_degree = 2*degree+1
        try:
            line_elements = get_line_elements(element)
        except ValueError:
            raise ValueError("FDMPC does not support the element %s" % V.ufl_element())
        Afdm = []  # sparse interval mass and stiffness matrices for each direction
        Dfdm = []  # tabulation of normal derivative of the FDM basis at the boundary for each direction
        for e in line_elements:
            if e.formdegree or is_dg:
                Afdm[:0], Dfdm[:0] = tuple(zip(fdm_setup_ipdg(e.ref_el, e.degree(), eta)))
            else:
                Afdm[:0], Dfdm[:0] = tuple(zip(fdm_setup_cg(e.ref_el, e.degree())))

        # coefficients w.r.t. the reference values
        coefficients, self.assembly_callables = self.assemble_coef(J, quad_degree)
        # set arbitrary non-zero coefficients for preallocation
        for coef in coefficients.values():
            with coef.dat.vec as cvec:
                cvec.set(1.0E0)

        bcflags = get_bc_flags(bcs, J)

        # preallocate by calling the assembly routine on a PREALLOCATOR Mat
        sizes = (V.dof_dset.layout_vec.getSizes(),)*2
        block_size = V.dof_dset.layout_vec.getBlockSize()
        prealloc = PETSc.Mat().create(comm=V.comm)
        prealloc.setType(PETSc.Mat.Type.PREALLOCATOR)
        prealloc.setSizes(sizes)
        prealloc.setUp()
        self.assemble_kron(prealloc, V, bcs, eta, coefficients, Afdm, Dfdm, bcflags)
        nnz = get_preallocation(prealloc, block_size * V.dof_dset.set.size)
        Pmat = PETSc.Mat().createAIJ(sizes, block_size, nnz=nnz, comm=V.comm)
        assemble_P = partial(self.assemble_kron, Pmat, V, bcs, eta,
                             coefficients, Afdm, Dfdm, bcflags)
        prealloc.destroy()
        return Pmat, assemble_P

    def assemble_kron(self, A, V, bcs, eta, coefficients, Afdm, Dfdm, bcflags):
        """
        Assemble the stiffness matrix in the FDM basis using Kronecker products of interval matrices

        :arg A: the :class:`PETSc.Mat` to assemble
        :arg V: the :class:`firedrake.FunctionSpace` of the form arguments
        :arg bcs: an iterable of :class:`firedrake.DirichletBCs`
        :arg eta: a ``float`` penalty parameter for the symmetric interior penalty method
        :arg coefficients: a ``dict`` mapping strings to :class:`firedrake.Functions` with the form coefficients
        :arg Afdm: the list with sparse interval matrices
        :arg Dfdm: the list with normal derivatives matrices
        :arg bcflags: the :class:`numpy.ndarray` with BC facet flags returned by `get_bc_flags`
        """
        Gq = coefficients.get("Gq")
        Bq = coefficients.get("Bq")
        Gq_facet = coefficients.get("Gq_facet")
        PT_facet = coefficients.get("PT_facet")

        imode = PETSc.InsertMode.ADD_VALUES
        lgmap = V.local_to_global_map(bcs)

        bsize = V.value_size
        ncomp = V.ufl_element().reference_value_size()
        sdim = (V.finat_element.space_dimension() * bsize) // ncomp  # dimension of a single component
        ndim = V.ufl_domain().topological_dimension()
        shift = get_shift(V.finat_element) % ndim

        index_cell, nel = glonum_fun(V.cell_node_map())
        index_coef, _ = glonum_fun(Gq.cell_node_map())
        flag2id = numpy.kron(numpy.eye(ndim, ndim, dtype=PETSc.IntType), [[1], [2]])

        # pshape is the shape of the DOFs in the tensor product
        pshape = tuple(Ak[0].size[0] for Ak in Afdm)
        if shift:
            assert ncomp == ndim
            pshape = [tuple(numpy.roll(pshape, -shift*k)) for k in range(ncomp)]

        if A.getType() != PETSc.Mat.Type.PREALLOCATOR:
            A.zeroEntries()
            for assemble_coef in self.assembly_callables:
                assemble_coef()

        # insert the identity in the Dirichlet rows and columns
        for row in V.dof_dset.lgmap.indices[lgmap.indices < 0]:
            A.setValue(row, row, 1.0E0, imode)

        # assemble zero-th order term separately, including off-diagonals (mixed components)
        # I cannot do this for hdiv elements as off-diagonals are not sparse, this is because
        # the FDM eigenbases for GLL(N) and GLL(N-1) are not orthogonal to each other
        use_diag_Bq = Bq is None or len(Bq.ufl_shape) != 2
        if not use_diag_Bq:
            bshape = Bq.ufl_shape
            # Be = Bhat kron ... kron Bhat
            Be = Afdm[0][0].copy()
            for k in range(1, ndim):
                Be = Be.kron(Afdm[k][0])

            aptr = numpy.arange(0, (bshape[0]+1)*bshape[1], bshape[1], dtype=PETSc.IntType)
            aidx = numpy.tile(numpy.arange(bshape[1], dtype=PETSc.IntType), bshape[0])
            for e in range(nel):
                # Ae = Be kron Bq[e]
                adata = numpy.sum(Bq.dat.data_ro[index_coef(e)], axis=0)
                Ae = PETSc.Mat().createAIJWithArrays(bshape, (aptr, aidx, adata), comm=PETSc.COMM_SELF)
                Ae = Be.kron(Ae)

                ie = index_cell(e)
                ie = numpy.repeat(ie*bsize, bsize) + numpy.tile(numpy.arange(bsize, dtype=ie.dtype), len(ie))
                rows = lgmap.apply(ie)
                set_submat_csr(A, Ae, rows, imode)
                Ae.destroy()
            Be.destroy()
            Bq = None

        # assemble the second order term and the zero-th order term if any,
        # discarding mixed derivatives and mixed components
        for e in range(nel):
            ie = numpy.reshape(index_cell(e), (ncomp//bsize, -1))
            je = index_coef(e)
            bce = bcflags[e]

            # get second order coefficient on this cell
            mue = numpy.atleast_1d(numpy.sum(Gq.dat.data_ro[je], axis=0))
            if Bq is not None:
                # get zero-th order coefficient on this cell
                bqe = numpy.atleast_1d(numpy.sum(Bq.dat.data_ro[je], axis=0))

            for k in range(ncomp):
                # permutation of axes with respect to the first vector component
                axes = numpy.roll(numpy.arange(ndim), -shift*k)
                # for each component: compute the stiffness matrix Ae
                muk = mue[k] if len(mue.shape) == 2 else mue
                bck = bce[k] if len(bce.shape) == 2 else bce
                fbc = numpy.dot(bck, flag2id)

                # Ae = mue[k][0] Ahat + bqe[k] Bhat
                Be = Afdm[axes[0]][0].copy()
                Ae = Afdm[axes[0]][1+fbc[0]].copy()
                Ae.scale(muk[0])
                if Bq is not None:
                    Ae.axpy(bqe[k], Be)

                if ndim > 1:
                    # Ae = Ae kron Bhat + mue[k][1] Bhat kron Ahat
                    Ae = Ae.kron(Afdm[axes[1]][0])
                    Ae.axpy(muk[1], Be.kron(Afdm[axes[1]][1+fbc[1]]))
                    if ndim > 2:
                        # Ae = Ae kron Bhat + mue[k][2] Bhat kron Bhat kron Ahat
                        Be = Be.kron(Afdm[axes[1]][0])
                        Ae = Ae.kron(Afdm[axes[2]][0])
                        Ae.axpy(muk[2], Be.kron(Afdm[axes[2]][1+fbc[2]]))

                rows = lgmap.apply(ie[0]*bsize+k if bsize == ncomp else ie[k])
                set_submat_csr(A, Ae, rows, imode)
                Ae.destroy()
                Be.destroy()

        # assemble SIPG interior facet terms if the normal derivatives have been set up
        if any(Dk is not None for Dk in Dfdm):
            if ndim < V.ufl_domain().geometric_dimension():
                raise NotImplementedError("SIPG on immersed meshes is not implemented")
            index_facet, local_facet_data, nfacets = get_interior_facet_maps(V)
            index_coef, _, _ = get_interior_facet_maps(Gq_facet or Gq)
            rows = numpy.zeros((2, sdim), dtype=PETSc.IntType)
            for e in range(nfacets):
                # for each interior facet: compute the SIPG stiffness matrix Ae
                ie = index_facet(e)
                je = numpy.reshape(index_coef(e), (2, -1))
                lfd = local_facet_data(e)
                idir = lfd // 2

                if PT_facet:
                    icell = numpy.reshape(lgmap.apply(ie), (2, ncomp, -1))
                    iord0 = numpy.insert(numpy.delete(numpy.arange(ndim), idir[0]), 0, idir[0])
                    iord1 = numpy.insert(numpy.delete(numpy.arange(ndim), idir[1]), 0, idir[1])
                    je = je[[0, 1], lfd]
                    Pfacet = PT_facet.dat.data_ro_with_halos[je]
                    Gfacet = Gq_facet.dat.data_ro_with_halos[je]
                else:
                    Gfacet = numpy.sum(Gq.dat.data_ro_with_halos[je], axis=1)

                for k in range(ncomp):
                    axes = numpy.roll(numpy.arange(ndim), -shift*k)
                    Dfacet = Dfdm[axes[0]]
                    if Dfacet is None:
                        continue

                    if PT_facet:
                        k0 = iord0[k] if shift != 1 else ndim-1-iord0[-k-1]
                        k1 = iord1[k] if shift != 1 else ndim-1-iord1[-k-1]
                        Piola = Pfacet[[0, 1], [k0, k1]]
                        mu = Gfacet[[0, 1], idir]
                    else:
                        if len(Gfacet.shape) == 3:
                            mu = Gfacet[[0, 1], [k, k], idir]
                        elif len(Gfacet.shape) == 2:
                            mu = Gfacet[[0, 1], idir]
                        else:
                            mu = Gfacet

                    offset = Dfacet.shape[0]
                    Adense = numpy.zeros((2*offset, 2*offset), dtype=PETSc.RealType)
                    dense_indices = []
                    for j, jface in enumerate(lfd):
                        j0 = j * offset
                        j1 = j0 + offset
                        jj = j0 + (offset-1) * (jface % 2)
                        dense_indices.append(jj)
                        for i, iface in enumerate(lfd):
                            i0 = i * offset
                            i1 = i0 + offset
                            ii = i0 + (offset-1) * (iface % 2)

                            sij = 0.5E0 if i == j else -0.5E0
                            if PT_facet:
                                smu = [sij*numpy.dot(numpy.dot(mu[0], Piola[i]), Piola[j]),
                                       sij*numpy.dot(numpy.dot(mu[1], Piola[i]), Piola[j])]
                            else:
                                smu = sij*mu

                            Adense[ii, jj] += eta * sum(smu)
                            Adense[i0:i1, jj] -= smu[i] * Dfacet[:, iface % 2]
                            Adense[ii, j0:j1] -= smu[j] * Dfacet[:, jface % 2]

                    Ae = numpy_to_petsc(Adense, dense_indices, diag=False)
                    if ndim > 1:
                        # assume that the mesh is oriented
                        Ae = Ae.kron(Afdm[axes[1]][0])
                        if ndim > 2:
                            Ae = Ae.kron(Afdm[axes[2]][0])

                    if bsize == ncomp:
                        icell = numpy.reshape(lgmap.apply(k+bsize*ie), (2, -1))
                        rows[0] = pull_axis(icell[0], pshape, idir[0])
                        rows[1] = pull_axis(icell[1], pshape, idir[1])
                    else:
                        assert pshape[k0][idir[0]] == pshape[k1][idir[1]]
                        rows[0] = pull_axis(icell[0][k0], pshape[k0], idir[0])
                        rows[1] = pull_axis(icell[1][k1], pshape[k1], idir[1])

                    set_submat_csr(A, Ae, rows, imode)
                    Ae.destroy()
        A.assemble()

    def assemble_coef(self, J, quad_deg, discard_mixed=True, cell_average=True):
        """
        Return the coefficients of the Jacobian form arguments and their gradient with respect to the reference coordinates.

        :arg J: the Jacobian bilinear form
        :arg quad_deg: the quadrature degree used for the coefficients
        :arg discard_mixed: discard entries in second order coefficient with mixed derivatives and mixed components
        :arg cell_average: to return the coefficients as DG_0 Functions

        :returns: a 2-tuple of
            coefficients: a dictionary mapping strings to :class:`firedrake.Functions` with the coefficients of the form,
            assembly_callables: a list of assembly callables for each coefficient of the form
        """
        coefficients = {}
        assembly_callables = []

        mesh = J.ufl_domain()
        gdim = mesh.geometric_dimension()
        tdim = mesh.topological_dimension()
        Finv = ufl.JacobianInverse(mesh)
        dx = firedrake.dx(degree=quad_deg)

        if cell_average:
            family = "Discontinuous Lagrange" if tdim == 1 else "DQ"
            degree = 0
        else:
            family = "Quadrature"
            degree = quad_deg

        # extract coefficients directly from the bilinear form
        args_J = J.arguments()
        integrals_J = J.integrals_by_type("cell")
        mapping = args_J[0].ufl_element().mapping().lower()
        if mapping == 'identity':
            Piola = None
        elif mapping == 'covariant piola':
            Piola = Finv.T
            Piola = Piola * firedrake.Constant(numpy.flipud(numpy.identity(tdim)), domain=mesh)
        elif mapping == 'contravariant piola':
            sign = ufl.diag(firedrake.Constant([-1]+[1]*(tdim-1), domain=mesh))
            Piola = ufl.Jacobian(mesh)*sign/ufl.JacobianDeterminant(mesh)
            if tdim < gdim:
                Piola *= 1-2*mesh.cell_orientations()
        else:
            raise NotImplementedError("Unsupported element mapping %s" % mapping)

        # get second order coefficient
        ref_grad = [ufl.variable(ufl.grad(t)) for t in args_J]
        if Piola:
            replace_grad = {ufl.grad(t): ufl.dot(Piola, ufl.dot(dt, Finv)) for t, dt in zip(args_J, ref_grad)}
        else:
            replace_grad = {ufl.grad(t): ufl.dot(dt, Finv) for t, dt in zip(args_J, ref_grad)}

        alpha = expand_derivatives(sum([diff(diff(ufl.replace(i.integrand(), replace_grad),
                                             ref_grad[0]), ref_grad[1]) for i in integrals_J]))

        # get zero-th order coefficent
        ref_val = [ufl.variable(t) for t in args_J]
        if Piola:
            dummy_element = ufl.TensorElement("DQ", cell=mesh.ufl_cell(), degree=1, shape=Piola.ufl_shape)
            dummy_Piola = ufl.Coefficient(ufl.FunctionSpace(mesh, dummy_element))
            replace_val = {t: ufl.dot(dummy_Piola, s) for t, s in zip(args_J, ref_val)}
        else:
            replace_val = {t: s for t, s in zip(args_J, ref_val)}

        beta = expand_derivatives(sum([diff(diff(ufl.replace(i.integrand(), replace_val),
                                            ref_val[0]), ref_val[1]) for i in integrals_J]))
        if Piola:
            beta = ufl.replace(beta, {dummy_Piola: Piola})

        G = alpha
        if discard_mixed:
            # discard mixed derivatives and mixed components
            if len(G.ufl_shape) == 2:
                G = ufl.diag_vector(G)
            else:
                Gshape = G.ufl_shape
                Gshape = Gshape[:len(Gshape)//2]
                G = ufl.as_tensor(numpy.reshape([G[i+i] for i in numpy.ndindex(Gshape)], (Gshape[0], -1)))
            Qe = ufl.TensorElement(family, mesh.ufl_cell(), degree=degree, quad_scheme="default", shape=G.ufl_shape)
        else:
            Qe = ufl.TensorElement(family, mesh.ufl_cell(), degree=degree, quad_scheme="default", shape=G.ufl_shape, symmetry=True)

        # assemble second order coefficient
        Q = firedrake.FunctionSpace(mesh, Qe)
        q = firedrake.TestFunction(Q)
        Gq = firedrake.Function(Q)
        coefficients["Gq"] = Gq
        assembly_callables.append(partial(firedrake.assemble, inner(G, q)*dx, Gq))

        # assemble zero-th order coefficient
        if not isinstance(beta, Zero):
            if Piola:
                # keep diagonal
                beta = ufl.diag_vector(beta)
            shape = beta.ufl_shape
            Qe = ufl.FiniteElement(family, mesh.ufl_cell(), degree=degree, quad_scheme="default")
            if shape:
                Qe = ufl.TensorElement(Qe, shape=shape)
            Q = firedrake.FunctionSpace(mesh, Qe)
            q = firedrake.TestFunction(Q)
            Bq = firedrake.Function(Q)
            coefficients["Bq"] = Bq
            assembly_callables.append(partial(firedrake.assemble, inner(beta, q)*dx, Bq))

        if Piola:
            # make DGT functions with the second order coefficient
            # and the Piola tensor for each side of each facet
            extruded = mesh.cell_set._extruded
            dS_int = firedrake.dS_h(degree=quad_deg) + firedrake.dS_v(degree=quad_deg) if extruded else firedrake.dS(degree=quad_deg)
            ele = ufl.BrokenElement(ufl.FiniteElement("DGT", mesh.ufl_cell(), 0))
            area = ufl.FacetArea(mesh)

            replace_grad = {ufl.grad(t): ufl.dot(dt, Finv) for t, dt in zip(args_J, ref_grad)}
            alpha = expand_derivatives(sum([diff(diff(ufl.replace(i.integrand(), replace_grad),
                                                 ref_grad[0]), ref_grad[1]) for i in integrals_J]))
            vol = abs(ufl.JacobianDeterminant(mesh))
            G = vol * alpha
            G = ufl.as_tensor([[[G[i, k, j, k] for i in range(G.ufl_shape[0])] for j in range(G.ufl_shape[2])] for k in range(G.ufl_shape[3])])

            Q = firedrake.TensorFunctionSpace(mesh, ele, shape=G.ufl_shape)
            q = firedrake.TestFunction(Q)
            Gq_facet = firedrake.Function(Q)
            coefficients["Gq_facet"] = Gq_facet
            assembly_callables.append(partial(firedrake.assemble, ((inner(q('+'), G('+')) + inner(q('-'), G('-')))/area)*dS_int, Gq_facet))

            PT = Piola.T
            Q = firedrake.TensorFunctionSpace(mesh, ele, shape=PT.ufl_shape)
            q = firedrake.TestFunction(Q)
            PT_facet = firedrake.Function(Q)
            coefficients["PT_facet"] = PT_facet
            assembly_callables.append(partial(firedrake.assemble, ((inner(q('+'), PT('+')) + inner(q('-'), PT('-')))/area)*dS_int, PT_facet))
        return coefficients, assembly_callables


def pull_axis(x, pshape, idir):
    """permute x by reshaping into pshape and moving axis idir to the front"""
    return numpy.reshape(numpy.moveaxis(numpy.reshape(x.copy(), pshape), idir, 0), x.shape)


def set_submat_csr(A_global, A_local, global_rows, imode):
    indptr, indices, data = A_local.getValuesCSR()
    for i, row in enumerate(global_rows.flat):
        i0 = indptr[i]
        i1 = indptr[i+1]
        A_global.setValues(row, global_rows.flat[indices[i0:i1]], data[i0:i1], imode)


def numpy_to_petsc(A_numpy, dense_indices, diag=True):
    # Create a SeqAIJ Mat from a dense matrix using the diagonal and a subset of rows and columns
    # If dense_indices is empty, then also include the off-diagonal corners of the matrix
    n = A_numpy.shape[0]
    nbase = int(diag) + len(dense_indices)
    nnz = numpy.full((n,), nbase, dtype=PETSc.IntType)
    if dense_indices:
        nnz[dense_indices] = n
    else:
        nnz[[0, -1]] = 2

    imode = PETSc.InsertMode.INSERT
    A_petsc = PETSc.Mat().createAIJ(A_numpy.shape, nnz=nnz, comm=PETSc.COMM_SELF)
    if diag:
        for j, ajj in enumerate(A_numpy.diagonal()):
            A_petsc.setValue(j, j, ajj, imode)

    if dense_indices:
        idx = numpy.arange(n, dtype=PETSc.IntType)
        for j in dense_indices:
            A_petsc.setValues(j, idx, A_numpy[j], imode)
            A_petsc.setValues(idx, j, A_numpy[:][j], imode)
    else:
        A_petsc.setValue(0, n-1, A_numpy[0][-1], imode)
        A_petsc.setValue(n-1, 0, A_numpy[-1][0], imode)

    A_petsc.assemble()
    return A_petsc


def sym_eig(A, B):
    """
    numpy version of `scipy.linalg.eigh`
    """
    L = numpy.linalg.cholesky(B)
    Linv = numpy.linalg.inv(L)
    C = numpy.dot(Linv, numpy.dot(A, Linv.T))
    Z, W = numpy.linalg.eigh(C)
    V = numpy.dot(Linv.T, W)
    return Z, V


def semhat(elem, rule):
    """
    Construct Laplacian stiffness and mass matrices

    :arg elem: the element
    :arg rule: quadrature rule

    :returns: 5-tuple of
        Ahat: stiffness matrix
        Bhat: mass matrix
        Jhat: tabulation of the shape functions on the quadrature nodes
        Dhat: tabulation of the first derivative of the shape functions on the quadrature nodes
        xhat: nodes of the element
    """
    basis = elem.tabulate(1, rule.get_points())
    Jhat = basis[(0,)]
    Dhat = basis[(1,)]
    what = rule.get_weights()
    Ahat = numpy.dot(numpy.multiply(Dhat, what), Dhat.T)
    Bhat = numpy.dot(numpy.multiply(Jhat, what), Jhat.T)
    xhat = numpy.array([list(x.get_point_dict().keys())[0][0] for x in elem.dual_basis()])
    return Ahat, Bhat, Jhat, Dhat, xhat


def fdm_setup(ref_el, degree):
    from FIAT.gauss_lobatto_legendre import GaussLobattoLegendre
    from FIAT.quadrature import GaussLegendreQuadratureLineRule
    elem = GaussLobattoLegendre(ref_el, degree)
    rule = GaussLegendreQuadratureLineRule(ref_el, degree+1)
    Ahat, Bhat, _, _, _ = semhat(elem, rule)
    Sfdm = numpy.eye(Ahat.shape[0])
    if Sfdm.shape[0] > 2:
        rd = (0, -1)
        kd = slice(1, -1)
        _, Sfdm[kd, kd] = sym_eig(Ahat[kd, kd], Bhat[kd, kd])
        Skk = Sfdm[kd, kd]
        Srr = Sfdm[numpy.ix_(rd, rd)]
        Sfdm[kd, rd] = numpy.dot(Skk, numpy.dot(numpy.dot(Skk.T, Bhat[kd, rd]), -Srr))

    # Facet normal derivatives
    basis = elem.tabulate(1, ref_el.get_vertices())
    Dfacet = basis[(1,)]
    Dfacet[:, 0] = -Dfacet[:, 0]
    Dfdm = numpy.dot(Sfdm.T, Dfacet)
    return Ahat, Bhat, Sfdm, Dfdm


def fdm_setup_cg(ref_el, degree):
    """
    Setup for the fast diagonalization method for continuous Lagrange
    elements. Compute the FDM eigenvector basis and the sparsified interval
    stiffness and mass matrices.

    :arg ref_el: UFC cell
    :arg degree: polynomial degree

    :returns: 3-tuple of:
        Afdm: a list of :class:`PETSc.Mats` with the sparse interval matrices
        Sfdm.T * Bhat * Sfdm, and bcs(Sfdm.T * Ahat * Sfdm) for every combination of either
        natural or strong Dirichlet BCs on each endpoint, where Sfdm is the tabulation
        of Dirichlet eigenfunctions on the GLL nodes,
        Dfdm: None.
    """
    def apply_strong_bcs(Ahat, bc0, bc1):
        k0 = 0 if bc0 == 1 else 1
        k1 = Ahat.shape[0] if bc1 == 1 else -1
        kk = slice(k0, k1)
        A = Ahat.copy()
        a = A.diagonal().copy()
        A[kk, kk] = 0.0E0
        numpy.fill_diagonal(A, a)
        return numpy_to_petsc(A, [0, A.shape[0]-1])

    Ahat, Bhat, Sfdm, _ = fdm_setup(ref_el, degree)
    A = numpy.dot(Sfdm.T, numpy.dot(Ahat, Sfdm))
    B = numpy.dot(Sfdm.T, numpy.dot(Bhat, Sfdm))
    Afdm = [numpy_to_petsc(B, [])]
    for bc1 in range(2):
        for bc0 in range(2):
            Afdm.append(apply_strong_bcs(A, bc0, bc1))
    return Afdm, None


def fdm_setup_ipdg(ref_el, degree, eta):
    """
    Setup for the fast diagonalization method for the IP-DG formulation.
    Compute the FDM eigenvector basis, its normal derivative and the
    sparsified interval stiffness and mass matrices.

    :arg ref_el: UFC cell
    :arg degree: polynomial degree
    :arg eta: penalty coefficient as a `float`

    :returns: 2-tuple of:
        Afdm: a list of :class:`PETSc.Mats` with the sparse interval matrices
        Sfdm.T * Bhat * Sfdm, and bcs(Sfdm.T * Ahat * Sfdm) for every combination of either
        natural or weak Dirichlet BCs on each endpoint, where Sfdm is the tabulation
        of Dirichlet eigenfunctions on the GLL nodes,
        Dfdm: the tabulation of the normal derivatives of the Dirichlet eigenfunctions.
    """
    def apply_weak_bcs(Ahat, Dfacet, bcs, eta):
        Abc = Ahat.copy()
        for j in (0, -1):
            if bcs[j] == 1:
                Abc[:, j] -= Dfacet[:, j]
                Abc[j, :] -= Dfacet[:, j]
                Abc[j, j] += eta
        return numpy_to_petsc(Abc, [0, Abc.shape[0]-1])

    Ahat, Bhat, Sfdm, Dfdm = fdm_setup(ref_el, degree)
    A = numpy.dot(Sfdm.T, numpy.dot(Ahat, Sfdm))
    B = numpy.dot(Sfdm.T, numpy.dot(Bhat, Sfdm))
    Afdm = [numpy_to_petsc(B, [])]
    for bc1 in range(2):
        for bc0 in range(2):
            Afdm.append(apply_weak_bcs(A, Dfdm, (bc0, bc1), eta))
    return Afdm, Dfdm


@lru_cache(maxsize=10)
def get_interior_facet_maps(V):
    """
    Extrude V.interior_facet_node_map and V.ufl_domain().interior_facets.local_facet_dat

    :arg V: a :class:`FunctionSpace`

    :returns: the 3-tuple of
        facet_to_nodes_fun: maps interior facets to the nodes of the two cells sharing it,
        local_facet_data_fun: maps interior facets to the local facet numbering in the two cells sharing it,
        nfacets: the total number of interior facets owned by this process
    """
    mesh = V.ufl_domain()
    intfacets = mesh.interior_facets
    facet_to_cells = intfacets.facet_cell_map.values
    local_facet_data = intfacets.local_facet_dat.data_ro

    facet_node_map = V.interior_facet_node_map()
    facet_to_nodes = facet_node_map.values
    nbase = facet_to_nodes.shape[0]

    if mesh.cell_set._extruded:
        facet_offset = facet_node_map.offset
        local_facet_data_h = numpy.array([5, 4], local_facet_data.dtype)

        cell_node_map = V.cell_node_map()
        cell_to_nodes = cell_node_map.values_with_halo
        cell_offset = cell_node_map.offset

        nelv = cell_node_map.values.shape[0]
        layers = facet_node_map.iterset.layers_array
        itype = cell_offset.dtype
        shift_h = numpy.array([[0], [1]], itype)

        if mesh.variable_layers:
            nv = 0
            to_base = []
            to_layer = []
            for f, cells in enumerate(facet_to_cells):
                istart = max(layers[cells, 0])
                iend = min(layers[cells, 1])
                nz = iend-istart-1
                nv += nz
                to_base.append(numpy.full((nz,), f, itype))
                to_layer.append(numpy.arange(nz, dtype=itype))

            nh = layers[:, 1]-layers[:, 0]-2
            to_base.append(numpy.repeat(numpy.arange(len(nh), dtype=itype), nh))
            to_layer += [numpy.arange(nf, dtype=itype) for nf in nh]

            to_base = numpy.concatenate(to_base)
            to_layer = numpy.concatenate(to_layer)
            nfacets = nv + sum(nh[:nelv])

            local_facet_data_fun = lambda e: local_facet_data[to_base[e]] if e < nv else local_facet_data_h
            facet_to_nodes_fun = lambda e: facet_to_nodes[to_base[e]] + to_layer[e]*facet_offset if e < nv else numpy.reshape(cell_to_nodes[to_base[e]] + numpy.kron(to_layer[e]+shift_h, cell_offset), (-1,))
        else:
            nelz = layers[0, 1]-layers[0, 0]-1
            nv = nbase * nelz
            nh = nelv * (nelz-1)
            nfacets = nv + nh

            local_facet_data_fun = lambda e: local_facet_data[e//nelz] if e < nv else local_facet_data_h
            facet_to_nodes_fun = lambda e: facet_to_nodes[e//nelz] + (e % nelz)*facet_offset if e < nv else numpy.reshape(cell_to_nodes[(e-nv)//(nelz-1)] + numpy.kron(((e-nv) % (nelz-1))+shift_h, cell_offset), (-1,))
    else:
        facet_to_nodes_fun = lambda e: facet_to_nodes[e]
        local_facet_data_fun = lambda e: local_facet_data[e]
        nfacets = nbase

    return facet_to_nodes_fun, local_facet_data_fun, nfacets


@lru_cache(maxsize=10)
def glonum_fun(node_map):
    """
    Return a function that maps each topological entity to its nodes and the total number of entities.

    :arg node_map: a :class:`pyop2.Map` mapping entities to their nodes, including ghost entities.

    :returns: a 2-tuple with the map and the number of cells owned by this process
    """
    nelv = node_map.values.shape[0]
    if node_map.offset is None:
        return lambda e: node_map.values_with_halo[e], nelv
    else:
        layers = node_map.iterset.layers_array
        if layers.shape[0] == 1:
            nelz = layers[0, 1]-layers[0, 0]-1
            nel = nelz*nelv
            return lambda e: node_map.values_with_halo[e//nelz] + (e % nelz)*node_map.offset, nel
        else:
            nelz = layers[:, 1]-layers[:, 0]-1
            nel = sum(nelz[:nelv])
            to_base = numpy.repeat(numpy.arange(node_map.values_with_halo.shape[0], dtype=node_map.offset.dtype), nelz)
            to_layer = numpy.concatenate([numpy.arange(nz, dtype=node_map.offset.dtype) for nz in nelz])
            return lambda e: node_map.values_with_halo[to_base[e]] + to_layer[e]*node_map.offset, nel


@lru_cache(maxsize=10)
def glonum(node_map):
    """
    Return an array with the nodes of each topological entity of a certain kind.

    :arg node_map: a :class:`pyop2.Map` mapping entities to their nodes, including ghost entities.

    :returns: a :class:`numpy.ndarray` whose rows are the nodes for each cell
    """
    if node_map.offset is None:
        return node_map.values_with_halo
    else:
        layers = node_map.iterset.layers_array
        if layers.shape[0] == 1:
            nelz = layers[0, 1]-layers[0, 0]-1
            to_layer = numpy.tile(numpy.arange(nelz, dtype=node_map.offset.dtype), len(node_map.values_with_halo))
        else:
            nelz = layers[:, 1]-layers[:, 0]-1
            to_layer = numpy.concatenate([numpy.arange(nz, dtype=node_map.offset.dtype) for nz in nelz])
        return numpy.repeat(node_map.values_with_halo, nelz, axis=0) + numpy.kron(to_layer.reshape((-1, 1)), node_map.offset)


@lru_cache(maxsize=10)
def get_bc_flags(bcs, J):
    # Return boundary condition flags on each cell facet
    # 0 => natural, do nothing
    # 1 => strong / weak Dirichlet
    V = J.arguments()[0].function_space()
    mesh = V.ufl_domain()

    if mesh.cell_set._extruded:
        layers = mesh.cell_set.layers_array
        nelv, nfacet, _ = mesh.cell_to_facets.data_with_halos.shape
        if layers.shape[0] == 1:
            nelz = layers[0, 1]-layers[0, 0]-1
            nel = nelv*nelz
        else:
            nelz = layers[:, 1]-layers[:, 0]-1
            nel = sum(nelz)
        # extrude cell_to_facets
        cell_to_facets = numpy.zeros((nel, nfacet+2, 2), dtype=mesh.cell_to_facets.data.dtype)
        cell_to_facets[:, :nfacet, :] = numpy.repeat(mesh.cell_to_facets.data_with_halos, nelz, axis=0)

        # get a function with a single node per facet
        # mark interior facets by assembling a surface integral
        dS_int = firedrake.dS_h(degree=0) + firedrake.dS_v(degree=0)
        DGT = firedrake.FunctionSpace(mesh, "DGT", 0)
        v = firedrake.TestFunction(DGT)
        w = firedrake.assemble((v('+')+v('-'))*dS_int)

        # mark the bottom and top boundaries with DirichletBCs
        markers = (-2, -4)
        subs = ("bottom", "top")
        bc_h = [firedrake.DirichletBC(DGT, marker, sub) for marker, sub in zip(markers, subs)]
        [bc.apply(w) for bc in bc_h]

        # index the function with the extruded cell_node_map
        marked_facets = w.dat.data_ro_with_halos[glonum(DGT.cell_node_map())]

        # complete the missing pieces of cell_to_facets
        interior = marked_facets > 0
        cell_to_facets[interior, :] = [1, -1]
        topbot = marked_facets < 0
        cell_to_facets[topbot, 0] = 0
        cell_to_facets[topbot, 1] = marked_facets[topbot].astype(cell_to_facets.dtype)
    else:
        cell_to_facets = mesh.cell_to_facets.data_with_halos

    flags = cell_to_facets[:, :, 0]
    sub = cell_to_facets[:, :, 1]

    maskall = []
    comp = dict()
    for bc in bcs:
        if isinstance(bc, firedrake.DirichletBC):
            labels = comp.get(bc._indices, ())
            bs = bc.sub_domain
            if bs == "on_boundary":
                maskall.append(bc._indices)
            elif bs == "bottom":
                labels += (-2,)
            elif bs == "top":
                labels += (-4,)
            else:
                labels += bs if type(bs) == tuple else (bs,)
            comp[bc._indices] = labels

    # The Neumann integral may still be present but it's zero
    J = expand_derivatives(J)
    # Assume that every facet integral in the Jacobian imposes a
    # weak Dirichlet BC on all components
    # TODO add support for weak component BCs
    # FIXME for variable layers there is inconsistency between
    # ds_t/ds_b and DirichletBC(V, ubc, "top/bottom").
    # the labels here are the ones that DirichletBC would use
    for it in J.integrals():
        itype = it.integral_type()
        if itype.startswith("exterior_facet"):
            index = ()
            labels = comp.get(index, ())
            bs = it.subdomain_id()
            if bs == "everywhere":
                if itype == "exterior_facet_bottom":
                    labels += (-2,)
                elif itype == "exterior_facet_top":
                    labels += (-4,)
                else:
                    maskall.append(index)
            else:
                labels += bs if type(bs) == tuple else (bs,)
            comp[index] = labels

    labels = comp.get((), ())
    labels = list(set(labels))
    fbc = numpy.isin(sub, labels).astype(PETSc.IntType)

    if () in maskall:
        fbc[sub >= -1] = 1
    fbc[flags != 0] = 0

    others = set(comp.keys()) - {()}
    if others:
        # We have bcs on individual vector components
        fbc = numpy.tile(fbc, (V.value_size, 1, 1))
        for j in range(V.value_size):
            key = (j,)
            labels = comp.get(key, ())
            labels = list(set(labels))
            fbc[j] |= numpy.isin(sub, labels)
            if key in maskall:
                fbc[j][sub >= -1] = 1

        fbc = numpy.transpose(fbc, (1, 0, 2))
    return fbc

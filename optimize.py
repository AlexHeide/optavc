import shutil
import os
import copy

import psi4

from .molecule import Molecule
from .singlepoint import Calculation
from .findifcalcs import Gradient
from .xtpl import xtpl_wrapper, energy_correction, order_high_low


class Optimization(Calculation):

    def __init__(self, molecule, input_obj, options, xtpl_inputs=None):
        super().__init__(molecule, input_obj, options)
        self.step_molecules = []
        self.xtpl_inputs = xtpl_inputs
        self.paths = []  # safety measure. No gradients should share the same path object

    def run(self, restart_iteration=0, user_xtpl_restart=None):
        
        # copy step if restart would have overwritten some steps
        self.copy_old_steps(restart_iteration, user_xtpl_restart)

        for iteration in range(self.options.maxiter):
            self.step_molecules.append(self.molecule)
            # compute the gradient for the current molecule --
            # the path for gradient computation is set to "STEP0x"

            print(f"\n====================Beginning new step {iteration}======================\n") 

            if self.options.xtpl:
                xtpl_gradients = self.create_xtpl_gradients(iteration, restart_iteration,
                                                            user_xtpl_restart)
                ref_energy, grad = self.run_xtpl_gradients(iteration, restart_iteration,
                                                           xtpl_gradients)
            else:
                grad_obj = self.create_opt_gradient(iteration)
                grad = self.run_gradient(iteration, restart_iteration, grad_obj)
                ref_energy = grad_obj.get_reference_energy()

            try:
                # put the gradient, energy, and molecule for the current step in psi::Environment
                # before calling psi4.optking()
                psi4.core.set_gradient(grad)
                psi4.core.set_variable('CURRENT ENERGY', ref_energy)
                psi4_mol_obj = self.molecule.cast_to_psi4_molecule_object()

                if self.options.point_group is not None:  # otherwise autodetect
                    psi4_mol_obj.reset_point_group(self.options.point_group)
                psi4.core.set_legacy_molecule(psi4_mol_obj)
                optking_exit_code = psi4.core.optking()

                # optking has put the next step geometry in psi::Environment,
                # so we can grab a copy and cast it
                # from psi4.Molecule() to Molecule()
                self.molecule = Molecule(psi4.core.get_legacy_molecule())
                if optking_exit_code == psi4.core.PsiReturnType.EndLoop:
                    psi4.core.print_out("Optimizer: Optimization complete!")
                    break
                elif optking_exit_code == psi4.core.PsiReturnType.Failure:
                    raise Exception("Optimizer: Optimization failed!")

                # We finished a step. Certainly, hessian reading should be disabled now!
                psi4.core.set_local_option('OPTKING', 'CART_HESS_READ', False)
                psi4.core.set_legacy_molecule(None)
            except Exception as e:
                print("An errror was encountered while using psi4 to take a step")
                print(str(e))
                raise

        if self.options.mpi:
            from .mpi4py_iface import slay
            slay()  # kill all workers before exiting

        print("\n\n OPTIMIZATION HAS FINISHED!\n\n")
        return True

    def run_gradient(self, iteration, restart_iteration, grad_obj, xtpl_reap=False,
                     force_resub=True):
        """ run a single gradient for an optimization. Reaping if iteration, restart_iteration,
        and xtpl_reap indicate this is possible

        Parameters
        ----------
        iteration: int
        restart_iteration: int
            indicates (with iteration) whether to sow/run or just reap previous energies
        xtpl_reap : bool, optional
            not the same as options.xtpl_restart
        grad_obj: findifcalcs.Gradient
        force_resub: bool, optional
            If the FIRST reap fails, resubmit all jobs to cluster for any gradient that "should"
            have already been completed based on iteration, restart_iteration, and xtpl_reap

        Returns
        -------
        grad: psi4.core.Matrix

        Notes
        -----
        Reassemble gradients as possible. Always restart based on iteration number
        If the we're rechecking the same input file in xtpl_procedure, xtpl_reap must be set

        """

        try:
            if iteration < restart_iteration:
                grad = grad_obj.reap(force_resub)  # could be 1 or more gradients
            elif xtpl_reap:
                grad = grad_obj.reap(force_resub)
            else:
                self.enforce_unique_paths(grad_obj)
                grad = grad_obj.compute_result()
        except RuntimeError as e:
            if xtpl_reap:
                print(str(e))
                print(f"""[CRITICAL] - could not compute gradient at step {iteration} \
                        sub-step {grad_obj.path}""")
            else:
                print(str(e))
                print(f"[CRITICAL] - could not compute gradient at step {iteration}")
            raise
        except FileNotFoundError:
            print(f"Missing an output file: {self.options.output_name}")
            raise
        except ValueError as e:
            print(str(e))
            raise

        return grad

    def create_opt_gradient(self, iteration):
        """  Create Gradient with path and name updated by iteration

        Parameters
        ----------
        iteration: int

        Returns
        -------
        grad_obj : findifcalcs.Gradient

        """

        options = copy.deepcopy(self.options)
        options.name = f"{self.options.name}--{iteration:02d}"
        step_path = f"STEP{iteration:>02d}"
        return Gradient(self.molecule, self.inp_file_obj, options, step_path)

    def create_xtpl_gradients(self, iteration, restart_iteration, user_xtpl_restart=None):
        """ Uses xtpl_wrapper to create each gradient object needed in the xtpl procedure
        and determine which gradients may be reaped and which may be calculated.

        Parameters
        ----------
        iteration: int
        restart_iteration: int
            which iteration shall we begin calculating at (0 based indexing)
        user_xtpl_restart: bool
            xtpl_restart parameter from optavc.run_optavc. Begin calculations at low_corr. reap
            high_corr

        Returns
        -------
        list[tuple(Gradient, bool)]: the Gradient object ready to be run and a flag for whether
        the gradient must be computed or may be reaped.

        """

        run_grad = []

        for index, grad_obj in enumerate(xtpl_wrapper("GRADIENT", self.molecule,
                                                      self.xtpl_inputs, self.options, path=".",
                                                      iteration=iteration)):

            # [2, 2] -> grab the low correlation, small basis calculations in single input file
            # [1, 3] -> do all low correlation calculations in single output file
            if self.options.xtpl_input_style == [2, 2]:
                separate_idx = 2
            else:
                separate_idx = 1

            # want to set sow = False if iteration < restart_iteration
            # if index not in [0, 2]
            # if xtpl_reap and index == 0
            acceptable_index = index not in [0, separate_idx]
            low_corr_restart = user_xtpl_restart and index == 0 and iteration == restart_iteration

            if acceptable_index or low_corr_restart:
                restart = True
            else:
                restart = False

            run_grad.append((grad_obj, restart))

        return run_grad

    def run_xtpl_gradients(self, iteration, restart_iteration, xtpl_gradients):
        """ Calls run_gradient for each gradient in the xtpl procedure and assembles into 1, final
        gradient

        Parameters
        ----------
        iteration: int
        restart_iteration: int
            which iteration shall we begin calculating at (0 based indexing)

        Returns
        -------
        final_en, final_grad: int, Union[Psi4.core.Matrix, Psi4.core.Vector]

        """

        gradients = []
        ref_energies = []

        for grad_obj, restart in xtpl_gradients:

            grad = self.run_gradient(iteration, restart_iteration, xtpl_reap=restart,
                                     grad_obj=grad_obj)

            gradients.append(grad)
            ref_energies.append(grad_obj.get_reference_energy())

        basis_sets = self.options.xtpl_basis_sets
        # These are "correlation gradients" i.e. energy_regex should correspond
        # only to correlation energy

        # Different orders for input_styles unify back to high corr -> low corr and large basis ->
        # small basis
        ordered_en, ordered_grads = order_high_low(gradients, ref_energies,
                                                   self.options.xtpl_input_style)
        
        final_en, final_grad, low_CBS_e = energy_correction(basis_sets, ordered_grads, ordered_en)

        print(
            f"\n\n=====================================XTPL=====================================\n")
        print(f"xtpl gradient:\n {str(final_grad.np)}")
        print(f"mp2/tqz{low_CBS_e}\t\t\t using helgaker 2 pt xtpl\n\n")
        print(f"CCSD - mp2 {ref_energies[0] - ref_energies[1]}")
        print(f"xtpl energy: {final_en}")
        print(f"energies {ordered_en}")
        # chr(10) is ascii \n
        print(f"gradients:\n {chr(10).join([str(i.np) for i in ordered_grads])}")
        print(
            f"\n=====================================XTPL=====================================\n\n")

        return final_en, final_grad

    def copy_old_steps(self, restart_iteration, xtpl_restart):
        """ If a directory is going to be overwritten by a restart copy the directories to backup 
        and prevent steps > restart_iteration are not immediately submitted.  

        """

        dir_test = f'{self.path}/STEP{restart_iteration:0>2d}'
        if xtpl_restart:
            dir_test = f'{dir_test}/low_corr'

        # if only the high_corr exists when restarting and xtpl_restart was not specified,
        # the high_corr jobs will be rurun and a copy of all steps will be made.

        if os.path.exists(dir_test):

            itr = 0
            # determine the number of directories of the form STEPXX exist. What number to
            # start at. itr will exit loop as STEP(Max+1).
            while os.path.exists(f'{self.path}/STEP{itr:>02d}'):
                itr += 1

            restart_itr = 1
            while os.path.exists(f'{self.path}/{restart_itr}_opt'):
                restart_itr += 1
            restart_itr -= 1  # counts too high

            # Move highest indexed group up. Delete. move previous up to replace delete.
            # Delete STEP0X for X >= restart index

            if restart_itr:
                for index in range(restart_itr, 0, -1):  # stop before 0.
                    shutil.copytree(f'{index}_opt',
                                    f'{index + 1}_opt')
                    # copy tree must be able to perform a mkdir for python <= 3.8
                    shutil.rmtree(f'{index}_opt')

            for step_idx in range(itr):
                shutil.copytree(f'{self.path}/STEP{step_idx:>02d}',
                                f'{self.path}/1_opt/STEP{step_idx:>02d}')

                # Keep STEP(restart) if xtpl_restart. Otherwise remove all >= restart_iteration
                if step_idx >= restart_iteration:
                    if step_idx == restart_iteration and xtpl_restart:
                        shutil.rmtree(f'{self.path}/STEP{step_idx:>02d}/low_corr')
                    else:
                        shutil.rmtree(f'{self.path}/STEP{step_idx:>02d}')

            shutil.copyfile(f'{self.path}/output.dat', f'{self.path}/1_opt/output.dat')
            with open(f'{self.path}/output.dat', 'w+'):
                pass  # clear file

    def enforce_unique_paths(self, grad_obj):
        """ Ensure that for an optimization no two gradients can be run in the same directory. Keep
        a list of paths used in a optimization up to date.

        Method should be called whenever a new Gradient in created and before it is run

        Parameters
        ----------
        grad_obj: Gradient
            The newly created Gradient object

        Notes
        -----
        This is a warning for future development: If the gradient's path is not updated with each
        step, collect_failures() will allow new steps to be taken in the same directory. When
        optavc checks the relevant output files, it will find them to be completed and run
        another calculation this will dump new jobs onto the cluster each time.

        Raises
        ------
        ValueError: This should not be caught. Indicates that changes
        """
        if grad_obj.path in self.paths:
            raise ValueError("Gradient's path matches gradient for a previous step. Cannot run. "
                             "This can cause failure in FindifCalc's collect_failures() method")
        else:
            self.paths.append(grad_obj.path)


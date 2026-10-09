/* ModelBuilder first-party product composition. UNLICENSED pending owner selection.
 * The public extension menu API does not expose MenubarFileMenu in Code - OSS 1.105.
 * This module only places existing extension commands; all behavior remains there.
 */
import { localize } from '../../../../nls.js';
import { MenuId, MenuRegistry } from '../../../../platform/actions/common/actions.js';
import { ContextKeyExpr } from '../../../../platform/contextkey/common/contextkey.js';

const projectOpen = ContextKeyExpr.has('modelbuilder.projectOpen');
MenuRegistry.appendMenuItem(MenuId.MenubarFileMenu, {
	group: '3_model_recipe', order: 1,
	command: { id: 'modelbuilder.importRecipeBundle', title: localize('modelbuilder.importRecipe', 'Import Recipe…'), precondition: projectOpen }
});
MenuRegistry.appendMenuItem(MenuId.MenubarFileMenu, {
	group: '3_model_recipe', order: 2,
	command: { id: 'modelbuilder.exportRecipeBundle', title: localize('modelbuilder.exportRecipe', 'Export Recipe…'), precondition: projectOpen }
});

MenuRegistry.appendMenuItem(MenuId.MenubarFileMenu, {
	group: '3_model_recipe', order: 3,
	command: { id: 'modelbuilder.discoverPrivateRecipe', title: localize('modelbuilder.discoverPrivateRecipe', 'Discover Private Recipes…'), precondition: projectOpen }
});
MenuRegistry.appendMenuItem(MenuId.MenubarFileMenu, {
	group: '3_model_recipe', order: 4,
	command: { id: 'modelbuilder.publishPrivateRecipe', title: localize('modelbuilder.publishPrivateRecipe', 'Publish Recipe Privately…'), precondition: projectOpen }
});
